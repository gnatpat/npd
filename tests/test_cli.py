import subprocess

import pytest

from deploy.cli import build_parser
from deploy.commands import diff, install, list_apps, remove
from deploy.paths import Paths
from deploy.runner import RecordingRunner
from tests.test_commands import POKEMON_TOML, answer, make_repo


@pytest.fixture(autouse=True)
def _fake_health_check(monkeypatch):
    """Same reasoning as test_commands.py's fixture of the same name: these
    tests exercise the list/diff/remove orchestration, not real service
    liveness, and this file has no access to that autouse fixture since it
    lives in a different module. Without this, install() below would burn
    wait_healthy's full 15s timeout against a RecordingRunner that never
    actually starts a listening process."""
    monkeypatch.setattr("deploy.commands.wait_healthy", lambda *a, **k: True)


def test_parser_accepts_every_documented_command():
    parser = build_parser()
    for argv in (
        ["install", "pokemon"],
        ["update", "pokemon"],
        ["update", "--all"],
        ["list"],
        ["list", "--fetch"],
        ["diff"],
        ["diff", "pokemon"],
        ["restart", "pokemon"],
        ["logs", "pokemon"],
        ["logs", "pokemon", "-f"],
        ["remove", "pokemon"],
        ["remove", "pokemon", "--purge"],
        ["dev"],
        ["dev", "--prefix", "--build", "--port", "9000"],
    ):
        assert parser.parse_args(argv) is not None


def test_list_reports_name_port_and_route(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    apps = list_apps(paths=paths, runner=RecordingRunner())
    assert [(a.name, a.port, a.route) for a in apps] == [("pokemon", 8151, "/pokemon/")]


def test_list_fetch_reports_commits_behind(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    runner = RecordingRunner(stdout={"rev-list": "3\n"})
    apps = list_apps(paths=paths, runner=runner, fetch=True)
    assert apps[0].behind == 3


def test_list_without_fetch_does_not_hit_the_network(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    runner = RecordingRunner()
    apps = list_apps(paths=paths, runner=runner)
    assert apps[0].behind is None
    # Match the actual fetch invocation ("git ... fetch --quiet"), not a bare
    # "fetch" substring: this test's own tmp_path (derived from the test
    # name, which contains "fetch") is embedded in every git command's `-C
    # <path>` argument, so a bare substring check false-positives on the
    # path rather than detecting a real network call.
    assert not runner.ran("fetch --quiet")


def test_list_is_empty_when_nothing_is_installed(tmp_path):
    assert list_apps(paths=Paths.under(tmp_path), runner=RecordingRunner()) == []


def test_diff_writes_nothing(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    runner = RecordingRunner()
    assert diff("pokemon", paths=paths, runner=runner) == 0
    assert not paths.systemd_unit_file("pokemon").exists()
    assert runner.calls == []


def test_remove_stops_disables_and_deletes_generated_files(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    runner = RecordingRunner()
    remove("pokemon", paths=paths, runner=runner, purge=False, confirm=lambda m: True)
    assert runner.ran("systemctl stop")
    assert runner.ran("systemctl disable")
    assert not paths.systemd_unit_file("pokemon").exists()
    assert not paths.nginx_snippet_file("pokemon").exists()


def test_remove_keeps_the_clone_and_the_env_file(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    remove(
        "pokemon",
        paths=paths,
        runner=RecordingRunner(),
        purge=False,
        confirm=lambda m: True,
    )
    assert paths.clone_dir("pokemon").exists()
    assert paths.env_file("pokemon").exists()


def test_purge_deletes_the_clone_only_after_confirmation(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    remove(
        "pokemon",
        paths=paths,
        runner=RecordingRunner(),
        purge=True,
        confirm=lambda m: False,
    )
    assert paths.clone_dir("pokemon").exists()


def test_purge_deletes_the_clone_when_confirmed(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    remove(
        "pokemon",
        paths=paths,
        runner=RecordingRunner(),
        purge=True,
        confirm=lambda m: True,
    )
    assert not paths.clone_dir("pokemon").exists()
    assert not paths.env_file("pokemon").exists()


def test_a_config_error_is_reported_without_a_traceback(tmp_path, capsys):
    from deploy.cli import main

    paths = Paths.under(tmp_path)
    repo = paths.clone_dir("broken")
    repo.mkdir(parents=True)
    (repo / "deploy.toml").write_text('[app]\nname = "broken"\n')  # no [service]
    assert main(["--root", str(tmp_path), "diff", "broken"]) == 1
    assert "error:" in capsys.readouterr().err


def test_apply_failure_carries_what_already_took_effect():
    from deploy.reconcile import ReloadFailed

    exc = ReloadFailed("reload broke", actions_completed={"daemon-reload"})
    assert exc.actions_completed == {"daemon-reload"}


def test_apply_failure_reaches_the_user_via_main(monkeypatch, capsys):
    """test_apply_failure_carries_what_already_took_effect only exercises the
    exception constructor; it never drives cli.main(), so "does
    actions_completed actually reach the terminal" was untested. Drive it
    for real by making a command raise ReloadFailed and checking stderr."""
    from deploy.cli import main
    from deploy.reconcile import ReloadFailed

    def _boom(*args, **kwargs):
        raise ReloadFailed(
            "systemctl reload nginx failed", actions_completed={"daemon-reload"}
        )

    monkeypatch.setattr("deploy.cli.commands.diff", _boom)
    assert main(["diff", "pokemon"]) == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "daemon-reload" in err


def test_logs_goes_straight_to_subprocess_call_not_through_runner(monkeypatch):
    """logs() must not go through Runner: RealRunner captures stdout/stderr,
    which would make `deploy logs` print nothing and `deploy logs -f` hang
    forever capturing an endless stream with nothing on screen."""
    from deploy import commands

    calls = []
    monkeypatch.setattr(
        commands.subprocess, "call", lambda argv: calls.append(argv) or 0
    )
    assert commands.logs("pokemon", follow=False) == 0
    assert calls == [["journalctl", "-u", "pokemon", "-n", "50", "--no-pager"]]


def test_logs_follow_passes_dash_f_instead_of_no_pager(monkeypatch):
    from deploy import commands

    calls = []
    monkeypatch.setattr(
        commands.subprocess, "call", lambda argv: calls.append(argv) or 0
    )
    commands.logs("pokemon", follow=True)
    assert calls[0][-1] == "-f"
    assert "--no-pager" not in calls[0]


def test_remove_refuses_to_delete_a_unit_still_reported_active(tmp_path):
    """If systemctl stop did not actually take effect, deleting the unit
    would leave a running service invisible to `deploy list` (no port
    record, no unit file left to identify it)."""
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    runner = RecordingRunner(stdout={"is-active": "active\n"})
    with pytest.raises(ValueError, match="still active"):
        remove("pokemon", paths=paths, runner=runner, purge=False, confirm=lambda m: True)
    assert paths.systemd_unit_file("pokemon").exists()


def test_remove_proceeds_when_the_unit_was_never_active(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    runner = RecordingRunner(stdout={"is-active": "inactive\n"})
    remove("pokemon", paths=paths, runner=runner, purge=False, confirm=lambda m: True)
    assert not paths.systemd_unit_file("pokemon").exists()


def test_cli_rejects_a_path_traversal_app_name_before_touching_paths(tmp_path, capsys):
    """A name like "../../something" must never reach Paths.clone_dir /
    env_file, which just concatenate — the rejection has to happen in the
    CLI layer, before any command function runs."""
    from deploy.cli import main

    assert main(["--root", str(tmp_path), "remove", "../../etc", "--purge"]) == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "slashes" in err


def test_list_continues_past_a_broken_apps_config(tmp_path):
    """The command someone reaches for to find out what is wrong must not be
    the one that breaks first: one app with an invalid deploy.toml must not
    stop every other, healthy app from listing."""
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())

    broken = paths.clone_dir("broken")
    broken.mkdir(parents=True)
    (broken / "deploy.toml").write_text('[app]\nname = "broken"\n')  # no [service]
    paths.units.mkdir(parents=True, exist_ok=True)
    paths.systemd_unit_file("broken").write_text("not a real unit")

    apps = {a.name: a for a in list_apps(paths=paths, runner=RecordingRunner())}
    assert apps["pokemon"].route == "/pokemon/"
    assert apps["pokemon"].error is None
    assert apps["broken"].route is None
    assert apps["broken"].error is not None


def test_a_called_process_error_is_reported_without_a_traceback(monkeypatch, capsys):
    """IMPORTANT 4: a missing SSH key on `git clone`, a `systemctl` refusal,
    a failed build -- all raise CalledProcessError, and all are first-run
    shaped failures that must print a clean message, not a Python
    traceback."""
    from deploy.cli import main

    def _boom(*args, **kwargs):
        raise subprocess.CalledProcessError(
            7, ["git", "clone", "bad"], output="", stderr="Host key verification failed.\n"
        )

    monkeypatch.setattr("deploy.cli.commands.diff", _boom)
    assert main(["diff", "pokemon"]) == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "exit 7" in err
    assert "Host key verification failed" in err


def test_a_called_process_error_with_no_captured_stderr_still_reports_cleanly(
    monkeypatch, capsys
):
    """A streamed command (build steps, journalctl) never has exc.stderr;
    the handler must not choke on that, and must not print an empty line."""
    from deploy.cli import main

    def _boom(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["false"])

    monkeypatch.setattr("deploy.cli.commands.diff", _boom)
    assert main(["diff", "pokemon"]) == 1
    err = capsys.readouterr().err
    assert "error:" in err
    assert "exit 1" in err


def test_update_all_continues_past_a_broken_app_and_reports_failure(
    tmp_path, capsys, monkeypatch
):
    """IMPORTANT 5: `update --all` used to abort the whole loop via
    `max(<generator>)` on the first app to raise -- a DirtyRepo or
    ConfigError from one app must not silently skip updating the rest."""
    from deploy.cli import main

    # Route RealRunner to RecordingRunner so the healthy app's git/systemctl
    # calls never touch the real system.
    monkeypatch.setattr("deploy.cli.RealRunner", RecordingRunner)

    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())

    broken = paths.clone_dir("broken")
    broken.mkdir(parents=True)
    (broken / "deploy.toml").write_text('[app]\nname = "broken"\n')  # no [service]
    paths.units.mkdir(parents=True, exist_ok=True)
    paths.systemd_unit_file("broken").write_text("not a real unit")

    rc = main(["--root", str(tmp_path), "update", "--all"])

    assert rc != 0
    out, err = capsys.readouterr()
    assert "error: broken:" in err
    assert "pokemon: already up to date" in out


def test_update_all_with_no_installed_apps_succeeds(tmp_path):
    """max() over an empty generator used to raise ValueError; an explicit
    loop over zero apps should just do nothing and succeed."""
    from deploy.cli import main

    assert main(["--root", str(tmp_path), "update", "--all"]) == 0
