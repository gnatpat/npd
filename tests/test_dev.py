import subprocess
import sys
from pathlib import Path

import pytest

from deploy.config import parse_config
from deploy.dev import MissingSecrets, dev_command, dev_workdir, resolve_env, run_dev
from deploy.runner import RecordingRunner

WITH_SECRET = """
[service]
workdir = "server"
start = "uv run uvicorn main:app --port $PORT"
[env]
LOG_LEVEL = "info"
[secrets]
COLLECTION_PASSWORD = "the collection password"
"""


def cfg(toml: str = WITH_SECRET):
    return parse_config(toml, repo_name="pokemon")


def test_port_is_injected():
    env = resolve_env(cfg(), {"COLLECTION_PASSWORD": "x"}, port=8000)
    assert env["PORT"] == "8000"


def test_non_secret_env_comes_from_the_config():
    env = resolve_env(cfg(), {"COLLECTION_PASSWORD": "x"}, port=8000)
    assert env["LOG_LEVEL"] == "info"


def test_secrets_come_from_the_dotenv():
    env = resolve_env(cfg(), {"COLLECTION_PASSWORD": "hunter2"}, port=8000)
    assert env["COLLECTION_PASSWORD"] == "hunter2"


def test_a_missing_secret_fails_with_its_description():
    with pytest.raises(MissingSecrets) as exc:
        resolve_env(cfg(), {}, port=8000)
    assert exc.value.names == ["COLLECTION_PASSWORD"]
    assert "the collection password" in str(exc.value)


def test_the_process_environment_is_inherited():
    env = resolve_env(cfg(), {"COLLECTION_PASSWORD": "x"}, port=8000)
    assert "PATH" in env


def test_dev_start_is_preferred_when_present():
    c = cfg('[service]\nstart = "prod"\n[dev]\nstart = "dev --reload"\n')
    assert dev_command(c) == "dev --reload"


def test_production_start_is_used_when_there_is_no_dev_section():
    c = cfg('[service]\nstart = "prod"\n')
    assert dev_command(c) == "prod"


def test_workdir_is_the_service_workdir_under_the_repo():
    assert dev_workdir(cfg(), Path("/repo")) == Path("/repo/server")


def test_workdir_is_the_repo_root_when_unset():
    c = cfg('[service]\nstart = "run"\n')
    assert dev_workdir(c, Path("/repo")) == Path("/repo")


def test_a_static_app_has_no_start_command():
    # A static app now requires an [nginx] section (config.py evolved after
    # this brief was written); added here to keep parse_config happy while
    # preserving the test's actual intent below.
    c = cfg(
        '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n'
        '[nginx]\npath = "/site/"\n'
    )
    with pytest.raises(ValueError, match="static"):
        dev_command(c)


def test_static_dev_serves_with_sys_executable_not_a_bare_python(monkeypatch):
    # Regression: a bare "python" does not exist on the target server (only
    # python3). The static branch runs through subprocess.call directly (it
    # is a long-running foreground process the user watches, not a command
    # whose output the tool consumes -- runner.run would capture its output
    # and the user would see nothing until it exited), so monkeypatch
    # subprocess.call to record its argv rather than starting a server.
    c = cfg(
        '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n'
        '[nginx]\npath = "/site/"\n'
    )
    calls = []
    monkeypatch.setattr(
        "deploy.dev.subprocess.call",
        lambda argv: calls.append(argv) or 0,
    )
    runner = RecordingRunner()
    run_dev(c, Path("/repo"), port=8000, build=False, prefix=False, runner=runner)

    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == sys.executable
    assert argv[1:4] == ["-m", "http.server", "8000"]


def test_build_steps_stream_via_subprocess_call_not_runner(monkeypatch):
    """IMPORTANT 3's second site: the build loop had the same
    output-swallowing problem as commands._run_build -- a long build step's
    output must reach the terminal live, not be captured by Runner and
    discarded."""
    c = cfg('[service]\nstart = "run"\n[build]\nsteps = ["echo hi"]\n')
    calls = []
    monkeypatch.setattr(
        "deploy.dev.subprocess.call",
        lambda argv, **kw: calls.append(argv) or 0,
    )
    runner = RecordingRunner()
    run_dev(c, Path("/repo"), port=8000, build=True, prefix=False, runner=runner)
    assert calls[0] == ["echo", "hi"]
    assert not runner.ran("echo hi")


def test_a_failing_build_step_raises_instead_of_continuing_to_run_the_app(monkeypatch):
    """A failed build step must stop `deploy dev --build`, not fall through
    to running the app against a half-built tree."""
    c = cfg('[service]\nstart = "run"\n[build]\nsteps = ["false"]\n')
    monkeypatch.setattr("deploy.dev.subprocess.call", lambda argv, **kw: 1)
    with pytest.raises(subprocess.CalledProcessError):
        run_dev(c, Path("/repo"), port=8000, build=True, prefix=False, runner=RecordingRunner())
