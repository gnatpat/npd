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


def test_static_dev_serves_with_sys_executable_not_a_bare_python():
    # Regression: a bare "python" does not exist on the target server (only
    # python3). Assert on the argv the runner was asked to run, rather than
    # actually starting a server.
    c = cfg(
        '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n'
        '[nginx]\npath = "/site/"\n'
    )
    runner = RecordingRunner()
    run_dev(c, Path("/repo"), port=8000, build=False, prefix=False, runner=runner)

    assert len(runner.calls) == 1
    argv = runner.calls[0]
    assert argv[0] == sys.executable
    assert argv[1:4] == ["-m", "http.server", "8000"]
