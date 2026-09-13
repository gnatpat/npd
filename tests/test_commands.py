import re
import subprocess
from pathlib import Path

import pytest

from npd.commands import install, update
from npd.paths import MANAGED_HEADER, Paths
from npd.runner import RecordingRunner

POKEMON_TOML = """
[app]
name = "pokemon"
[service]
workdir = "server"
start = "uv run uvicorn main:app --host 127.0.0.1 --port $PORT"
port = 8151
[nginx]
path = "/pokemon/"
strip_prefix = false
[secrets]
COLLECTION_PASSWORD = "the password"
"""

STATIC_TOML = """
[app]
name = "boggle"
type = "static"
[build]
steps = []
output = "static"
[nginx]
path = "/boggle/"
"""


def make_repo(paths: Paths, name: str, toml: str, *, static: bool = False):
    """A pre-existing clone, which is what migration produces."""
    repo = paths.clone_dir(name)
    (repo / "server").mkdir(parents=True, exist_ok=True)
    (repo / "npd.toml").write_text(toml)
    if static:
        out = repo / "static"
        out.mkdir(exist_ok=True)
        (out / "index.html").write_text("hello")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x"],
        cwd=repo,
        check=True,
    )
    return repo


def answer(value="hunter2"):
    return lambda name, description: value


@pytest.fixture(autouse=True)
def _fake_health_check(monkeypatch):
    """These tests exercise orchestration (config -> render -> reconcile ->
    systemctl/secrets flow), not real service liveness — that is
    test_health.py's job. RecordingRunner never actually starts a listening
    process, so without this, wait_healthy's real TCP connect would fail
    every time and either burn its full timeout (real wall-clock seconds,
    times every install/update call in this file) or make an `install(...)
    == 0` assertion fail outright once the timeout elapses. Patch it to
    succeed immediately so these tests verify orchestration, not networking."""
    monkeypatch.setattr("npd.commands.wait_healthy", lambda *a, **k: True)


def test_install_writes_unit_and_nginx(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    assert install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer()) == 0
    assert paths.systemd_unit_file("pokemon").exists()
    assert paths.nginx_snippet_file("pokemon").exists()


def test_install_honours_the_pinned_port(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    assert (
        'Environment="PORT=8151"'
        in paths.systemd_unit_file("pokemon").read_text()
    )


def test_install_writes_prompted_secrets_at_0600(tmp_path):
    import stat

    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer("s3cret"))
    env_file = paths.env_file("pokemon")
    assert "COLLECTION_PASSWORD=s3cret" in env_file.read_text()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600


def test_install_links_enables_and_starts_the_unit(tmp_path):
    """A single `restart` replaces the old start/restart split: it starts a
    never-started unit exactly as well as it restarts a running one (see
    CRITICAL 1 in the final review — the old start/restart split, gated on
    first_install, is what let a retry after a partial failure register
    nothing with systemd while still reporting success)."""
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    runner = RecordingRunner()
    install("pokemon", paths=paths, runner=runner, prompt=answer())
    assert runner.ran("systemctl link")
    assert runner.ran("systemctl enable")
    assert runner.ran("systemctl restart")


def test_install_is_idempotent(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    before = paths.systemd_unit_file("pokemon").read_text()
    runner = RecordingRunner()
    install("pokemon", paths=paths, runner=runner, prompt=answer())
    assert paths.systemd_unit_file("pokemon").read_text() == before
    assert not runner.ran("reload nginx")


def test_a_retry_after_a_partial_failure_still_registers_and_restarts(tmp_path):
    """CRITICAL 1's regression test. apply_changes writes the unit file
    before link/enable/restart run, so a failure at any of those points
    (here, `systemctl enable` failing -- e.g. systemd refusing to enable
    over a stale unmanaged file from a missed migration step) leaves the
    unit file already in place on disk by the time the operator retries.
    Under the old code, first_install was computed from that file's
    existence, so the retry saw first_install=False, plan_app_changes found
    no diff, and link/enable/start were skipped entirely -- registering
    nothing with systemd while still printing success. Both attempts must
    actually run link/enable/restart."""
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)

    failing = RecordingRunner(results={"systemctl enable": 1})
    with pytest.raises(subprocess.CalledProcessError):
        install("pokemon", paths=paths, runner=failing, prompt=answer())
    assert failing.ran("systemctl link")
    assert failing.ran("systemctl enable")
    assert paths.systemd_unit_file("pokemon").exists()

    retry = RecordingRunner()
    assert install("pokemon", paths=paths, runner=retry, prompt=answer()) == 0
    assert retry.ran("systemctl link")
    assert retry.ran("systemctl enable")
    assert retry.ran("systemctl restart")


def test_install_of_a_static_app_publishes_and_writes_no_unit(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "boggle", STATIC_TOML, static=True)
    runner = RecordingRunner(stdout={"rev-parse": "abc123def4567890\n"})
    assert install("boggle", paths=paths, runner=runner, prompt=answer()) == 0
    assert (paths.static / "boggle-abc123def456").is_dir()
    assert not paths.systemd_unit_file("boggle").exists()
    assert (paths.static / "boggle" / "index.html").read_text() == "hello"


def test_a_failed_build_aborts_before_writing_anything(tmp_path):
    paths = Paths.under(tmp_path)
    toml = POKEMON_TOML + '\n[build]\nsteps = ["false"]\n'
    make_repo(paths, "pokemon", toml)
    runner = RecordingRunner(results={"false": 1})
    with pytest.raises(subprocess.CalledProcessError):
        install("pokemon", paths=paths, runner=runner, prompt=answer())
    assert not paths.systemd_unit_file("pokemon").exists()


def test_build_steps_stream_to_the_terminal_instead_of_going_through_runner(
    tmp_path, monkeypatch
):
    """IMPORTANT 3: a build step (npm install, a compiler...) can run for
    minutes, and its output is for the operator to watch live, not for this
    tool to consume. Going through Runner (RealRunner sets
    capture_output=True) would swallow it, leaving silence followed by a
    bare failure message with no diagnosis. _run_build must use
    subprocess.call directly, like logs() and the journal tail, and must
    not touch Runner for the build steps themselves."""
    paths = Paths.under(tmp_path)
    toml = POKEMON_TOML + '\n[build]\nsteps = ["echo hi"]\n'
    make_repo(paths, "pokemon", toml)

    calls = []
    monkeypatch.setattr(
        "npd.commands.subprocess.call",
        lambda argv, **kw: calls.append(argv) or 0,
    )
    runner = RecordingRunner()
    assert install("pokemon", paths=paths, runner=runner, prompt=answer()) == 0
    assert calls == [["echo", "hi"]]
    assert not runner.ran("echo hi")


def test_a_failing_build_step_raises_a_called_process_error(tmp_path):
    """The build no longer goes through Runner (which used to raise
    CalledProcessError itself via check=True), so _run_build must raise it
    manually on a non-zero exit for the existing failure handling to fire."""
    paths = Paths.under(tmp_path)
    toml = POKEMON_TOML + '\n[build]\nsteps = ["false"]\n'
    make_repo(paths, "pokemon", toml)
    with pytest.raises(subprocess.CalledProcessError):
        install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())


def test_a_failed_build_keeps_the_secret_so_it_need_not_be_retyped(tmp_path):
    """Secrets are collected before the build runs (so a fatal
    EnvironmentFile= is never handed to a unit that hasn't got one yet), and
    that ordering is deliberate even though it means a failed build has
    already written the env file: retyping a password after a broken build
    step is exactly the annoyance missing_secrets exists to avoid on the
    next run."""
    paths = Paths.under(tmp_path)
    toml = POKEMON_TOML + '\n[build]\nsteps = ["false"]\n'
    make_repo(paths, "pokemon", toml)
    runner = RecordingRunner(results={"false": 1})
    with pytest.raises(subprocess.CalledProcessError):
        install("pokemon", paths=paths, runner=runner, prompt=answer("s3cret"))
    assert not paths.systemd_unit_file("pokemon").exists()
    assert not paths.nginx_snippet_file("pokemon").exists()
    assert "COLLECTION_PASSWORD=s3cret" in paths.env_file("pokemon").read_text()


def test_a_route_collision_is_refused(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    other = POKEMON_TOML.replace('name = "pokemon"', 'name = "clash"').replace(
        "port = 8151", "port = 8199"
    )
    make_repo(paths, "clash", other)
    with pytest.raises(ValueError, match="/pokemon/"):
        install("clash", paths=paths, runner=RecordingRunner(), prompt=answer())


def test_update_with_no_changes_reports_up_to_date_and_does_not_restart(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    runner = RecordingRunner()
    assert update("pokemon", paths=paths, runner=runner, prompt=answer()) == 0
    assert not runner.ran("systemctl restart")


def test_update_with_the_same_commit_still_reports_up_to_date(tmp_path):
    """Pins CRITICAL 2's idempotence requirement: stamping NPD_COMMIT
    into the unit must not turn every update into "something changed" —
    only a genuine commit change should. Uses a real-looking, non-empty sha
    (RecordingRunner's unconfigured default is "", which is falsy and would
    trivially pass this) held fixed across both calls."""
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    sha = "abc123def4567890abc123def4567890abc123d"
    install(
        "pokemon",
        paths=paths,
        runner=RecordingRunner(stdout={"rev-parse": f"{sha}\n"}),
        prompt=answer(),
    )
    before = paths.systemd_unit_file("pokemon").read_text()
    assert f"NPD_COMMIT={sha}" in before

    runner = RecordingRunner(stdout={"rev-parse": f"{sha}\n"})
    assert update("pokemon", paths=paths, runner=runner, prompt=answer()) == 0
    assert paths.systemd_unit_file("pokemon").read_text() == before
    assert not runner.ran("systemctl restart")


class OneTimeMoveRunner(RecordingRunner):
    """Simulates a real repo more faithfully than MovingRunner (used
    elsewhere in this file): `rev-parse HEAD` returns `before` exactly once,
    then `after` forever afterwards — as a single real `git pull` would
    move HEAD from `before` to `after` and leave it there, rather than
    advancing on every single call."""

    def __init__(self, before: str, after: str):
        super().__init__()
        self._shas = [before] + [after] * 20

    def run(self, argv, *, cwd=None, env=None, check=True):
        argv = list(argv)
        if "rev-parse" in " ".join(argv):
            self.calls.append(argv)
            sha = self._shas.pop(0) if len(self._shas) > 1 else self._shas[0]
            return subprocess.CompletedProcess(argv, 0, stdout=f"{sha}\n", stderr="")
        return super().run(argv, cwd=cwd, env=env, check=check)


def test_a_failed_build_followed_by_a_clean_update_redeploys(tmp_path):
    """CRITICAL 2's regression test. A build failing after a successful
    pull must not be indistinguishable from "nothing to do": the unit on
    disk still carries the OLD commit, so a later update -- even one that
    finds nothing new to pull, because the earlier pull already happened --
    must see the stamped commit disagree with HEAD and redeploy, not print
    "already up to date" while the process keeps running the old code."""
    paths = Paths.under(tmp_path)
    repo = make_repo(paths, "pokemon", POKEMON_TOML)
    install(
        "pokemon",
        paths=paths,
        runner=RecordingRunner(stdout={"rev-parse": "sha1\n"}),
        prompt=answer(),
    )
    assert "NPD_COMMIT=sha1" in paths.systemd_unit_file("pokemon").read_text()

    # Simulate: new commits land (HEAD moves from sha1 to sha2) and the
    # build then fails.
    (repo / "npd.toml").write_text(POKEMON_TOML + '\n[build]\nsteps = ["false"]\n')
    with pytest.raises(subprocess.CalledProcessError):
        update(
            "pokemon",
            paths=paths,
            runner=OneTimeMoveRunner("sha1", "sha2"),
            prompt=answer(),
        )
    # Nothing was deployed: the unit on disk still says sha1.
    assert "NPD_COMMIT=sha1" in paths.systemd_unit_file("pokemon").read_text()

    # Fix the build and retry. pull_ff_only now finds nothing new (HEAD is
    # already sha2 and stays there) -- but the commit stamped on disk (sha1)
    # still disagrees with HEAD (sha2), so this must redeploy.
    (repo / "npd.toml").write_text(POKEMON_TOML)
    runner = RecordingRunner(stdout={"rev-parse": "sha2\n"})
    assert update("pokemon", paths=paths, runner=runner, prompt=answer()) == 0
    assert "NPD_COMMIT=sha2" in paths.systemd_unit_file("pokemon").read_text()
    assert runner.ran("systemctl restart")


def test_update_prompts_only_for_newly_declared_secrets(tmp_path):
    paths = Paths.under(tmp_path)
    repo = make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer("first"))

    asked = []

    def record(name, description):
        asked.append(name)
        return "second"

    (repo / "npd.toml").write_text(POKEMON_TOML + '\nNEW_SECRET = "another"\n')
    update("pokemon", paths=paths, runner=RecordingRunner(), prompt=record)
    assert asked == ["NEW_SECRET"]
    assert "COLLECTION_PASSWORD=first" in paths.env_file("pokemon").read_text()


class MovingRunner(RecordingRunner):
    """rev-parse returns a different sha each call, so pull_ff_only reports
    that new commits arrived without needing a real remote."""

    def __init__(self):
        super().__init__()
        self._n = 0

    def run(self, argv, *, cwd=None, env=None, check=True):
        argv = list(argv)
        if "rev-parse" in " ".join(argv):
            self._n += 1
            self.calls.append(argv)
            return subprocess.CompletedProcess(
                argv, 0, stdout=f"sha{self._n}\n", stderr=""
            )
        return super().run(argv, cwd=cwd, env=env, check=check)


_COMMIT_LINE = re.compile(r'Environment="NPD_COMMIT=[^"]+"\n')


def test_new_commits_restart_and_update_the_stamped_commit(tmp_path):
    """New commits must still force a restart even when nothing in
    npd.toml changed -- the running process is executing the old code
    until it is restarted. Under the CRITICAL 2 fix the unit is no longer
    byte-identical in this case (its NPD_COMMIT stamp moves forward,
    which is exactly the mechanism that fix relies on to detect a stale
    deploy later), so this pins "unchanged except for the commit stamp"
    rather than "byte-identical", which is what this test used to assert
    before that fix existed."""
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    before = paths.systemd_unit_file("pokemon").read_text()

    runner = MovingRunner()
    update("pokemon", paths=paths, runner=runner, prompt=answer())
    after = paths.systemd_unit_file("pokemon").read_text()

    assert after != before
    assert _COMMIT_LINE.search(after)
    assert _COMMIT_LINE.sub("", after) == _COMMIT_LINE.sub("", before)
    assert runner.ran("systemctl restart")


def test_a_foreign_unit_is_never_clobbered(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    paths.units.mkdir(parents=True, exist_ok=True)
    paths.systemd_unit_file("pokemon").write_text("[Service]\nExecStart=/hand/written\n")
    from npd.reconcile import ForeignFile

    with pytest.raises(ForeignFile):
        install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    assert "hand/written" in paths.systemd_unit_file("pokemon").read_text()


def test_a_failed_health_check_returns_1_and_leaves_the_service_running(
    tmp_path, monkeypatch
):
    """This is the branch an operator actually hits on a bad deploy: the
    unit was linked/enabled/started but the app never came up healthy. It
    must be reported (exit 1, journal tailed) without stopping the service —
    Restart=always means systemd will keep retrying, and npd must not
    make a bad deploy worse by tearing down what's there."""
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    # Overrides the autouse _fake_health_check stub for this test only.
    monkeypatch.setattr("npd.commands.wait_healthy", lambda *a, **k: False)

    # The journal tail is for the operator to read right now, so it goes
    # through subprocess.call directly rather than Runner (RealRunner would
    # capture and discard it). Record its argv instead of running it for
    # real.
    calls = []
    monkeypatch.setattr(
        "npd.commands.subprocess.call", lambda argv: calls.append(argv) or 0
    )

    runner = RecordingRunner()
    assert install("pokemon", paths=paths, runner=runner, prompt=answer()) == 1
    assert not runner.ran("systemctl stop")
    assert len(calls) == 1
    assert "journalctl" in calls[0]
    assert "-u" in calls[0] and "pokemon" in calls[0]


def test_a_second_install_reuses_a_clone_already_renamed_to_the_app_name(tmp_path):
    """boggle's repo is boggle-solver but its [app] name is boggle: the
    first install clones into ~/apps/boggle-solver then renames it to
    ~/apps/boggle. A second install must find that clone by its remote
    instead of cloning a fresh, orphaned ~/apps/boggle-solver — npd never
    deletes a clone, so a duplicate would sit there forever."""
    from npd.gitrepo import repo_url as compute_repo_url

    paths = Paths.under(tmp_path)
    make_repo(paths, "boggle-solver", STATIC_TOML, static=True)
    url = compute_repo_url("boggle-solver")
    runner = RecordingRunner(
        stdout={"remote get-url origin": url, "rev-parse": "abc123def4567890\n"}
    )

    assert install("boggle-solver", paths=paths, runner=runner, prompt=answer()) == 0
    assert paths.clone_dir("boggle").is_dir()
    assert not paths.clone_dir("boggle-solver").exists()

    assert install("boggle-solver", paths=paths, runner=runner, prompt=answer()) == 0
    assert not runner.ran("git clone")
    assert [p.name for p in paths.apps.iterdir()] == ["boggle"]


def test_existing_clone_matches_an_https_origin_against_an_ssh_url(tmp_path):
    """IMPORTANT 6. A clone whose origin is https (e.g. left over from a
    manual migration) must still be recognised as the same repo as the ssh
    URL this tool generates -- otherwise install clones a duplicate that
    the rename guard then refuses, leaving an orphan."""
    from npd.commands import _existing_clone

    paths = Paths.under(tmp_path)
    repo = paths.clone_dir("boggle-solver")
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    runner = RecordingRunner(
        stdout={"remote get-url origin": "https://github.com/gnatpat/boggle-solver.git\n"}
    )

    found = _existing_clone(
        "git@github.com:gnatpat/boggle-solver.git", paths=paths, runner=runner
    )
    assert found == repo


def test_existing_clone_skips_a_directory_with_no_origin_remote(tmp_path):
    """IMPORTANT 6. A git repo under ~/apps with no `origin` configured must
    not abort install for an unrelated reason -- it is simply not a match."""
    from npd.commands import _existing_clone

    paths = Paths.under(tmp_path)
    repo = paths.clone_dir("no-origin")
    repo.mkdir(parents=True)
    (repo / ".git").mkdir()
    runner = RecordingRunner(results={"no-origin remote get-url": 1})

    found = _existing_clone(
        "git@github.com:gnatpat/whatever.git", paths=paths, runner=runner
    )
    assert found is None


def test_remove_of_a_never_installed_app_reports_nothing_and_exits_nonzero(
    tmp_path, capsys
):
    """MINOR 7. A typo'd app name must be visible, not silently reported as
    "removed" with exit 0."""
    from npd.commands import remove

    paths = Paths.under(tmp_path)
    assert remove(
        "nope", paths=paths, runner=RecordingRunner(), purge=False, confirm=lambda m: True
    ) == 1
    assert "nothing to remove" in capsys.readouterr().err


def test_purge_of_a_static_app_deletes_the_published_build_output(tmp_path):
    """MINOR 7. --purge of a static app must also clean up
    /var/www/npd/<name> (the live symlink) and its versioned build
    directories -- otherwise they are left behind forever, since nothing
    else garbage-collects them."""
    from npd.commands import remove

    paths = Paths.under(tmp_path)
    make_repo(paths, "boggle", STATIC_TOML, static=True)
    runner = RecordingRunner(stdout={"rev-parse": "abc123def4567890\n"})
    install("boggle", paths=paths, runner=runner, prompt=answer())
    assert (paths.static / "boggle").is_symlink()
    assert (paths.static / "boggle-abc123def456").is_dir()

    remove("boggle", paths=paths, runner=RecordingRunner(), purge=True, confirm=lambda m: True)

    assert not (paths.static / "boggle").exists()
    assert not (paths.static / "boggle").is_symlink()
    assert not (paths.static / "boggle-abc123def456").exists()
    assert not paths.clone_dir("boggle").exists()


# --- terminal narration (previously-silent steps must be reported) --------


def test_install_narrates_writes_reloads_link_and_health_in_order(tmp_path, capsys):
    """The gap this whole change closes: on a real install, everything
    between the build finishing and the health check result used to be
    silent. Pin the exact lines and their order."""
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    capsys.readouterr()

    assert install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer()) == 0
    out = capsys.readouterr().out.splitlines()

    unit = str(paths.systemd_unit_file("pokemon"))
    nginx = str(paths.nginx_snippet_file("pokemon"))
    assert out == [
        "pokemon: writing config",
        f"  + {unit}",
        f"  + {nginx}",
        "pokemon: nginx -t passed, reloaded nginx",
        "pokemon: reloaded systemd",
        "pokemon: linked, enabled and restarted pokemon.service",
        "pokemon: waiting for port 8151 to accept connections …",
        "pokemon: healthy on port 8151",
    ]


def test_install_waits_by_url_when_a_health_path_is_configured(tmp_path, capsys):
    paths = Paths.under(tmp_path)
    toml = POKEMON_TOML.replace(
        'port = 8151\n', 'port = 8151\nhealth_path = "/pokemon/"\n'
    )
    make_repo(paths, "pokemon", toml)
    capsys.readouterr()

    assert install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer()) == 0
    out = capsys.readouterr().out.splitlines()
    assert "pokemon: waiting for 127.0.0.1:8151/pokemon/ …" in out


def test_update_that_only_changes_the_unit_does_not_report_an_nginx_reload(
    tmp_path, capsys
):
    """A new commit stamps NPD_COMMIT into the unit but never touches the
    rendered nginx snippet, so nginx must never be reloaded for it -- and
    the operator must be able to see, not just infer, that it wasn't."""
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    capsys.readouterr()

    runner = MovingRunner()
    assert update("pokemon", paths=paths, runner=runner, prompt=answer()) == 0
    out = capsys.readouterr().out.splitlines()

    unit = str(paths.systemd_unit_file("pokemon"))
    assert out[0] == "pokemon: writing config"
    assert out[1] == f"  ~ {unit}"
    assert not any("nginx" in line for line in out)
    assert "pokemon: reloaded systemd" in out


def test_update_with_no_config_change_reports_config_unchanged(tmp_path, capsys):
    """A static app's nginx snippet carries no commit stamp, so pulling new
    code that doesn't touch [nginx] leaves nothing to write. The reader
    must be told config was left alone on purpose, not left to wonder
    whether the step silently ran or was skipped."""
    paths = Paths.under(tmp_path)
    make_repo(paths, "boggle", STATIC_TOML, static=True)
    sha1 = "abc123def4567890abc123def4567890abc123d"
    sha2 = "def4567890abc123def4567890abc123def4567"
    install(
        "boggle",
        paths=paths,
        runner=RecordingRunner(stdout={"rev-parse": f"{sha1}\n"}),
        prompt=answer(),
    )
    capsys.readouterr()

    runner = OneTimeMoveRunner(sha1, sha2)
    assert update("boggle", paths=paths, runner=runner, prompt=answer()) == 0
    out = capsys.readouterr().out.splitlines()

    assert "boggle: config unchanged" in out
    assert not any("reloaded" in line for line in out)
    assert out[-1] == "boggle: published"


def test_restart_prints_restarted_and_healthy(tmp_path, capsys, monkeypatch):
    """restart() used to print nothing at all on success."""
    from npd.commands import restart

    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    capsys.readouterr()

    monkeypatch.setattr("npd.commands.wait_healthy", lambda *a, **k: True)
    assert restart("pokemon", paths=paths, runner=RecordingRunner()) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "pokemon: restarted pokemon.service",
        "pokemon: waiting for port 8151 to accept connections …",
        "pokemon: healthy on port 8151",
    ]


def test_remove_prints_the_files_it_deletes(tmp_path, capsys):
    from npd.commands import remove

    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    capsys.readouterr()

    unit = str(paths.systemd_unit_file("pokemon"))
    nginx = str(paths.nginx_snippet_file("pokemon"))
    assert remove(
        "pokemon", paths=paths, runner=RecordingRunner(), purge=False, confirm=lambda m: True
    ) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "pokemon: removing config"
    assert f"  - {unit}" in out
    assert f"  - {nginx}" in out
    assert out[-1] == "pokemon: removed"
    assert out.index(f"  - {unit}") < out.index("pokemon: removed")


def test_report_changes_picks_the_heading_verb_from_the_change_set(capsys):
    """The heading must describe what the changes actually are: all
    removals (remove()'s case) is not "writing", and a mix is neither
    "writing" nor "removing" outright."""
    from npd.commands import _report_changes
    from npd.paths import NGINX_SNIPPET, SYSTEMD_UNIT
    from npd.reconcile import Change

    unit_path = Path("/etc/npd/systemd/pokemon.service")
    nginx_path = Path("/etc/nginx/npd.d/pokemon.conf")

    _report_changes("pokemon", [Change(unit_path, "before", None, SYSTEMD_UNIT)])
    assert capsys.readouterr().out.splitlines()[0] == "pokemon: removing config"

    _report_changes("pokemon", [Change(unit_path, None, "after", SYSTEMD_UNIT)])
    assert capsys.readouterr().out.splitlines()[0] == "pokemon: writing config"

    _report_changes("pokemon", [Change(unit_path, "before", "after", SYSTEMD_UNIT)])
    assert capsys.readouterr().out.splitlines()[0] == "pokemon: writing config"

    _report_changes(
        "pokemon",
        [
            Change(unit_path, "before", "after", SYSTEMD_UNIT),
            Change(nginx_path, "before", None, NGINX_SNIPPET),
        ],
    )
    assert capsys.readouterr().out.splitlines()[0] == "pokemon: updating config"


def test_a_second_non_purge_remove_reports_leftovers_and_exits_nonzero(
    tmp_path, capsys
):
    """A second `remove` after the generated config is already gone (clone
    and env file still present) must not claim success: nothing was
    removed this time, and "config unchanged" is install/update's phrase,
    not remove's."""
    from npd.commands import remove

    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    assert remove(
        "pokemon", paths=paths, runner=RecordingRunner(), purge=False, confirm=lambda m: True
    ) == 0
    assert paths.clone_dir("pokemon").exists()
    capsys.readouterr()

    assert remove(
        "pokemon", paths=paths, runner=RecordingRunner(), purge=False, confirm=lambda m: True
    ) == 1
    out, err = capsys.readouterr()
    assert (
        "pokemon: nothing to remove — generated config is already gone "
        "(use --purge to also delete the clone and secrets)" in err
    )
    assert "config unchanged" not in out
    assert "removed" not in out


def test_a_second_purge_remove_still_cleans_up_the_clone_and_secrets(tmp_path):
    """--purge must still work once the generated config is already gone --
    that leftover clone/env file is exactly what --purge is for."""
    from npd.commands import remove

    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    remove(
        "pokemon", paths=paths, runner=RecordingRunner(), purge=False, confirm=lambda m: True
    )
    assert paths.clone_dir("pokemon").exists()

    assert remove(
        "pokemon", paths=paths, runner=RecordingRunner(), purge=True, confirm=lambda m: True
    ) == 0
    assert not paths.clone_dir("pokemon").exists()
    assert not paths.env_file("pokemon").exists()


def test_tail_journal_passes_quiet_flag(monkeypatch):
    """journalctl's own "Hint: You are currently not seeing messages from
    other users..." preamble is noise right after a FAILED health check;
    -q suppresses it."""
    from npd.commands import _tail_journal

    calls = []
    monkeypatch.setattr(
        "npd.commands.subprocess.call", lambda argv: calls.append(argv) or 0
    )
    _tail_journal("pokemon")
    assert len(calls) == 1
    assert "-q" in calls[0]
