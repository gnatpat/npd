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
