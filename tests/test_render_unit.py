from pathlib import Path

import pytest

from npd import settings
from npd.config import parse_config
from npd.paths import MANAGED_HEADER, NGINX_SNIPPET, SYSTEMD_UNIT, Paths
from npd.render import render, render_systemd_unit

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
    return render_systemd_unit(parse_config(toml, repo_name="pokemon"), port, PATHS)


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
    assert "EnvironmentFile=/srv/test/etc/npd/env/pokemon.env\n" in unit()


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
    assert f"User={settings.SERVICE_USER}\n" in out


def test_service_user_setting_actually_reaches_the_unit(monkeypatch):
    """settings.SERVICE_USER must not just exist -- it has to be the thing
    that actually decides the unit's `User=` line, not a value render.py
    happens to duplicate on its own. Without this, extracting the constant
    into settings.py could be purely decorative."""
    monkeypatch.setattr(settings, "SERVICE_USER", "someone-else")
    assert "User=someone-else\n" in unit()


def test_render_produces_unit_and_nginx_for_a_service():
    artifacts = render(parse_config(POKEMON, repo_name="pokemon"), 8151, PATHS)
    assert {(a.kind, a.path) for a in artifacts} == {
        (SYSTEMD_UNIT, Path("/srv/test/etc/npd/systemd/pokemon.service")),
        (NGINX_SNIPPET, Path("/srv/test/etc/nginx/npd.d/pokemon.conf")),
    }


def test_render_produces_only_a_unit_for_an_internal_service():
    cfg = parse_config('[service]\nstart = "run"\n', repo_name="internal")
    artifacts = render(cfg, 8201, PATHS)
    assert {a.path for a in artifacts} == {
        Path("/srv/test/etc/npd/systemd/internal.service")
    }
    assert {a.kind for a in artifacts} == {SYSTEMD_UNIT}


def test_render_produces_only_nginx_for_a_static_app():
    cfg = parse_config(
        '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n'
        '[nginx]\npath = "/boggle/"\n',
        repo_name="boggle",
    )
    artifacts = render(cfg, None, PATHS)
    assert {a.path for a in artifacts} == {
        Path("/srv/test/etc/nginx/npd.d/boggle.conf")
    }
    assert {a.kind for a in artifacts} == {NGINX_SNIPPET}


def test_rendering_a_service_without_a_port_is_a_programming_error():
    cfg = parse_config('[service]\nstart = "run"\n', repo_name="x")
    with pytest.raises(ValueError, match="port"):
        render(cfg, None, PATHS)


def test_env_value_with_space_is_quoted_and_survives_intact():
    cfg = parse_config(
        '[service]\nstart = "run"\n[env]\nGREETING = "hello there"\n',
        repo_name="test",
    )
    out = render_systemd_unit(cfg, 8000, PATHS)
    assert 'Environment="GREETING=hello there"\n' in out


def test_percent_in_env_value_is_escaped_for_systemd():
    cfg = parse_config(
        '[service]\nstart = "run"\n[env]\nMSG = "100%"\n',
        repo_name="test",
    )
    out = render_systemd_unit(cfg, 8000, PATHS)
    assert 'Environment="MSG=100%%"\n' in out


def test_percent_in_start_command_is_escaped_for_systemd():
    cfg = parse_config(
        '[service]\nstart = "serve --fmt %H"\n',
        repo_name="test",
    )
    out = render_systemd_unit(cfg, 8000, PATHS)
    assert "ExecStart=/bin/bash -c 'exec serve --fmt %%H'\n" in out


def test_commit_is_stamped_right_after_port_when_given():
    out = unit()  # unit()'s default port=8151, no commit passed -> None
    assert "NPD_COMMIT" not in out
    out = render_systemd_unit(
        parse_config(POKEMON, repo_name="pokemon"), 8151, PATHS, commit="a" * 40
    )
    assert f'Environment="PORT=8151"\nEnvironment="NPD_COMMIT={"a" * 40}"\n' in out


def test_no_commit_line_when_commit_is_none_or_empty():
    out = render_systemd_unit(
        parse_config(POKEMON, repo_name="pokemon"), 8151, PATHS, commit=None
    )
    assert "NPD_COMMIT" not in out
    out = render_systemd_unit(
        parse_config(POKEMON, repo_name="pokemon"), 8151, PATHS, commit=""
    )
    assert "NPD_COMMIT" not in out


def test_render_forwards_commit_into_the_unit():
    artifacts = render(parse_config(POKEMON, repo_name="pokemon"), 8151, PATHS, "b" * 40)
    (unit_artifact,) = [a for a in artifacts if a.kind is SYSTEMD_UNIT]
    assert f'NPD_COMMIT={"b" * 40}' in unit_artifact.contents


SANDBOXED = POKEMON.replace('[service]\n', '[service]\nsandbox = true\n')


def test_an_unsandboxed_unit_has_no_sandbox_lines():
    assert "ProtectHome" not in unit()


def test_a_sandboxed_unit_hides_home_except_the_whole_clone():
    out = unit(SANDBOXED)
    assert "ProtectHome=tmpfs\n" in out
    # The clone, not the service workdir: apps keep data anywhere in their clone.
    assert "BindPaths=/srv/test/apps/pokemon\n" in out


def test_a_sandboxed_unit_cannot_read_any_apps_secrets():
    # "-" so a box that has never stored a secret (no env dir) still starts.
    assert "InaccessiblePaths=-/srv/test/etc/npd/env\n" in unit(SANDBOXED)


def test_a_sandboxed_unit_is_read_only_elsewhere_and_resource_capped():
    out = unit(SANDBOXED)
    for line in (
        "ProtectSystem=strict",
        "PrivateTmp=yes",
        "NoNewPrivileges=yes",
        "MemoryMax=200M",
        "TasksMax=100",
        "CPUQuota=50%",
    ):
        assert f"{line}\n" in out
