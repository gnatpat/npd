import subprocess

import pytest

from deploy.commands import install, update
from deploy.paths import MANAGED_HEADER, Paths
from deploy.runner import RecordingRunner

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
    (repo / "deploy.toml").write_text(toml)
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
    monkeypatch.setattr("deploy.commands.wait_healthy", lambda *a, **k: True)


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
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    runner = RecordingRunner()
    install("pokemon", paths=paths, runner=runner, prompt=answer())
    assert runner.ran("systemctl link")
    assert runner.ran("systemctl enable")
    assert runner.ran("systemctl start")


def test_install_is_idempotent(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    before = paths.systemd_unit_file("pokemon").read_text()
    runner = RecordingRunner()
    install("pokemon", paths=paths, runner=runner, prompt=answer())
    assert paths.systemd_unit_file("pokemon").read_text() == before
    assert not runner.ran("reload nginx")


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


def test_update_prompts_only_for_newly_declared_secrets(tmp_path):
    paths = Paths.under(tmp_path)
    repo = make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer("first"))

    asked = []

    def record(name, description):
        asked.append(name)
        return "second"

    (repo / "deploy.toml").write_text(POKEMON_TOML + '\nNEW_SECRET = "another"\n')
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


def test_new_commits_restart_the_service_even_if_the_unit_is_unchanged(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    before = paths.systemd_unit_file("pokemon").read_text()

    runner = MovingRunner()
    update("pokemon", paths=paths, runner=runner, prompt=answer())

    assert paths.systemd_unit_file("pokemon").read_text() == before
    assert runner.ran("systemctl restart")


def test_a_foreign_unit_is_never_clobbered(tmp_path):
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    paths.units.mkdir(parents=True, exist_ok=True)
    paths.systemd_unit_file("pokemon").write_text("[Service]\nExecStart=/hand/written\n")
    from deploy.reconcile import ForeignFile

    with pytest.raises(ForeignFile):
        install("pokemon", paths=paths, runner=RecordingRunner(), prompt=answer())
    assert "hand/written" in paths.systemd_unit_file("pokemon").read_text()


def test_a_failed_health_check_returns_1_and_leaves_the_service_running(
    tmp_path, monkeypatch
):
    """This is the branch an operator actually hits on a bad deploy: the
    unit was linked/enabled/started but the app never came up healthy. It
    must be reported (exit 1, journal tailed) without stopping the service —
    Restart=always means systemd will keep retrying, and deploy must not
    make a bad deploy worse by tearing down what's there."""
    paths = Paths.under(tmp_path)
    make_repo(paths, "pokemon", POKEMON_TOML)
    # Overrides the autouse _fake_health_check stub for this test only.
    monkeypatch.setattr("deploy.commands.wait_healthy", lambda *a, **k: False)

    runner = RecordingRunner()
    assert install("pokemon", paths=paths, runner=runner, prompt=answer()) == 1
    assert not runner.ran("systemctl stop")
    assert runner.ran("journalctl")


def test_a_second_install_reuses_a_clone_already_renamed_to_the_app_name(tmp_path):
    """boggle's repo is boggle-solver but its [app] name is boggle: the
    first install clones into ~/apps/boggle-solver then renames it to
    ~/apps/boggle. A second install must find that clone by its remote
    instead of cloning a fresh, orphaned ~/apps/boggle-solver — deploy never
    deletes a clone, so a duplicate would sit there forever."""
    from deploy.gitrepo import repo_url as compute_repo_url

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
