from pathlib import Path

import pytest

from deploy.config import parse_config
from deploy.paths import MANAGED_HEADER, Paths
from deploy.render import render, render_unit

PATHS = Paths.under(Path("/srv/test"))

POKEMON = """
[app]
name = "pokemon"
[service]
workdir = "server"
start = "uv run uvicorn main:app --host 127.0.0.1 --port $PORT"
[nginx]
path = "/pokemon/"
strip_prefix = false
[env]
LOG_LEVEL = "info"
[secrets]
COLLECTION_PASSWORD = "the password"
"""


def unit(toml: str = POKEMON, port: int = 8151) -> str:
    return render_unit(parse_config(toml, repo_name="pokemon"), port, PATHS)


def test_unit_starts_with_the_managed_header():
    assert unit().startswith(MANAGED_HEADER)


def test_workdir_is_the_clone_plus_service_workdir():
    assert "WorkingDirectory=/srv/test/apps/pokemon/server\n" in unit()


def test_workdir_omits_the_subdir_when_unset():
    out = unit('[service]\nstart = "run"\n')
    assert "WorkingDirectory=/srv/test/apps/pokemon\n" in out


def test_path_is_explicit_not_a_login_shell():
    out = unit()
    assert (
        'Environment="PATH=/home/nathan/.local/bin:/usr/local/bin:/usr/bin:/bin"\n'
        in out
    )
    assert "bash -lc" not in out


def test_port_is_injected_as_an_environment_variable():
    assert 'Environment="PORT=8151"\n' in unit()


def test_non_secret_env_becomes_environment_lines():
    assert 'Environment="LOG_LEVEL=info"\n' in unit()


def test_secrets_come_from_an_environment_file():
    assert "EnvironmentFile=/srv/test/etc/deploy/env/pokemon.env\n" in unit()


def test_no_environment_file_when_no_secrets_are_declared():
    out = unit('[service]\nstart = "run"\n')
    assert "EnvironmentFile" not in out


def test_execstart_wraps_the_start_command_in_bash_exec():
    assert (
        "ExecStart=/bin/bash -c 'exec uv run uvicorn main:app "
        "--host 127.0.0.1 --port $PORT'\n" in unit()
    )


def test_unit_has_an_install_section_so_enable_works_on_a_linked_unit():
    assert "[Install]\nWantedBy=multi-user.target\n" in unit()


def test_restart_policy_is_always_with_one_second_backoff():
    out = unit()
    assert "Restart=always\n" in out
    assert "RestartSec=1\n" in out
    assert "User=nathan\n" in out


def test_render_produces_unit_and_nginx_for_a_service():
    files = render(parse_config(POKEMON, repo_name="pokemon"), 8151, PATHS)
    assert set(files) == {
        Path("/srv/test/etc/deploy/systemd/pokemon.service"),
        Path("/srv/test/etc/nginx/deploy.d/pokemon.conf"),
    }


def test_render_produces_only_a_unit_for_an_internal_service():
    cfg = parse_config('[service]\nstart = "run"\n', repo_name="internal")
    files = render(cfg, 8201, PATHS)
    assert set(files) == {Path("/srv/test/etc/deploy/systemd/internal.service")}


def test_render_produces_only_nginx_for_a_static_app():
    cfg = parse_config(
        '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n'
        '[nginx]\npath = "/boggle/"\n',
        repo_name="boggle",
    )
    files = render(cfg, None, PATHS)
    assert set(files) == {Path("/srv/test/etc/nginx/deploy.d/boggle.conf")}


def test_rendering_a_service_without_a_port_is_a_programming_error():
    cfg = parse_config('[service]\nstart = "run"\n', repo_name="x")
    with pytest.raises(ValueError, match="port"):
        render(cfg, None, PATHS)


def test_env_value_with_space_is_quoted_and_survives_intact():
    cfg = parse_config(
        '[service]\nstart = "run"\n[env]\nGREETING = "hello there"\n',
        repo_name="test",
    )
    out = render_unit(cfg, 8000, PATHS)
    assert 'Environment="GREETING=hello there"\n' in out


def test_percent_in_env_value_is_escaped_for_systemd():
    cfg = parse_config(
        '[service]\nstart = "run"\n[env]\nMSG = "100%"\n',
        repo_name="test",
    )
    out = render_unit(cfg, 8000, PATHS)
    assert 'Environment="MSG=100%%"\n' in out


def test_percent_in_start_command_is_escaped_for_systemd():
    cfg = parse_config(
        '[service]\nstart = "serve --fmt %H"\n',
        repo_name="test",
    )
    out = render_unit(cfg, 8000, PATHS)
    assert "ExecStart=/bin/bash -c 'exec serve --fmt %%H'\n" in out
