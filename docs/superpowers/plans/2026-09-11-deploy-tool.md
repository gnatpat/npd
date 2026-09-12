# `deploy` Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a `deploy` CLI that installs and updates small Python web services and static sites on the natpat.net droplet, generating their systemd units and nginx snippets instead of hand-writing them.

**Architecture:** Three layers pointing one way. *Config* parses `deploy.toml` into a validated `AppConfig` (pure). *Render* turns `(AppConfig, port, Paths)` into a `dict[Path, str]` of every generated file's exact contents (pure). *Reconcile* diffs that against disk, writes only what differs, and reloads only what the changes require (all IO, behind an injected `Runner`). There is no state file — the generated unit carries the assigned port and *is* the record.

**Tech Stack:** Python 3.11+, standard library only at runtime (`tomllib`, `http.server`, `subprocess`, `pathlib`). `pytest` for tests. `uv` for packaging and installation. systemd 245 and nginx 1.18 on Ubuntu 20.04.

**Spec:** `docs/superpowers/specs/2026-09-11-deploy-tool-design.md`

## Status — read this before executing anything

**Tasks 1-8 are implemented and merged.** `src/deploy/` is the source of
truth, NOT the code in those task sections. Their code blocks are the original
draft; twenty-three defects were found in review and fixed in the commits, and
the modules were then refactored. Do not re-implement Tasks 1-8 from this
document, and do not use their code as a reference for style or API — read the
source.

**Tasks 9-13 have been updated for the current API.** The refactor changed
these interfaces; the signatures below are correct as of commit `6bc5035`:

| Before | Now |
|---|---|
| `Paths.unit_file(name)` | `Paths.systemd_unit_file(name)` |
| `Paths.nginx_file(name)` | `Paths.nginx_snippet_file(name)` |
| `render_unit` / `render_nginx` | `render_systemd_unit` / `render_nginx_snippet` |
| `render(...) -> dict[Path, str]` | `render(...) -> tuple[Artifact, ...]` |
| `plan_changes(desired_dict)` | `plan_app_changes(name, artifacts, paths)` |
| `owned_files(name, paths)` | `owned_artifacts(name, paths) -> list[OwnedArtifact]` |
| classify by `"systemd" in str(path)` | `change.kind is SYSTEMD_UNIT` |
| `Environment=PORT=8151` | `Environment="PORT=8151"` (quoted) |

`Artifact(kind, path, contents)` and `OwnedArtifact(kind, path)` both live in
`deploy.render` / `deploy.reconcile` respectively. `ArtifactKind`,
`SYSTEMD_UNIT`, `NGINX_SNIPPET` and `ARTIFACT_KINDS` live in `deploy.paths`.

`NginxTestFailed` and `ReloadFailed` now share an `ApplyFailed` base carrying
`.actions_completed` (reloads that DID take effect before the failure) and
`.unrestored` (files rollback could not put back). The CLI should catch
`ApplyFailed` and report both — that is the whole point of their existing.

Config validation is also stricter than when this plan was written: `PORT` and
`PATH` are reserved in `[env]`/`[secrets]`, ports must be 1024-65535, app names
must be a single safe path segment, `nginx.path` is character-restricted and
may not be `/`, and `client_max_body_size` must match `^\d+[kKmMgG]?$`. Every
value used in Tasks 9-13 below already satisfies these.

## Global Constraints

- **Python >= 3.11** — required for `tomllib`. The server's system Python is 3.8; `uv` fetches its own interpreter, as it already does for blog (which requires >=3.13).
- **No runtime dependencies.** Standard library only. `pytest` is a dev dependency.
- **Managed header, exact text:** `# Managed by deploy — edits will be overwritten` — note the em dash (U+2014). Every generated file starts with this line. Reconcile refuses to overwrite any existing file whose first line is not this.
- **Port range: 8200–8299.** Pinned ports may fall outside it (blog 8080, pokemon 8151, crochet 8152 all do).
- **GitHub user: `gnatpat`.** `install <name>` resolves to `git@github.com:gnatpat/<name>.git`. Overridable via `DEPLOY_GITHUB_USER`.
- **Server user: `nathan`.** Units set `Environment=PATH=/home/nathan/.local/bin:/usr/local/bin:/usr/bin:/bin` explicitly — never a login shell.
- **Generated services bind `127.0.0.1`,** never `0.0.0.0`.
- **The tool never deletes a clone and never runs `git clean`.** App data lives inside clones today. Only `remove --purge` deletes, and it names what it will delete and asks first.
- **`sudo` is used only for `systemctl`, `nginx -t` and `nginx` reload.** Everything else runs as `nathan`.

---

### Task 1: Project scaffold, paths, and config parsing

**Files:**
- Create: `pyproject.toml`
- Create: `src/deploy/__init__.py`
- Create: `src/deploy/paths.py`
- Create: `src/deploy/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `Paths` (frozen dataclass with fields `apps`, `env`, `units`, `nginx`, `static`; classmethods `default() -> Paths` and `under(root: Path) -> Paths`). `MANAGED_HEADER: str`. `ConfigError(Exception)`. Frozen dataclasses `BuildConfig(steps: tuple[str, ...], workdir: str, output: str)`, `ServiceConfig(start: str, workdir: str, health_path: str | None, port: int | None)`, `NginxConfig(path: str, strip_prefix: bool, client_max_body_size: str | None)`, `AppConfig(name: str, type: str, build: BuildConfig | None, service: ServiceConfig | None, nginx: NginxConfig | None, dev_start: str | None, env: dict[str, str], secrets: dict[str, str])`. Function `parse_config(text: str, *, repo_name: str) -> AppConfig`.

- [ ] **Step 1: Create the package scaffold**

`pyproject.toml`:

```toml
[project]
name = "deploy"
version = "0.1.0"
description = "Thin deployment tool for natpat.net"
requires-python = ">=3.11"
dependencies = []

[project.scripts]
deploy = "deploy.cli:main"

[dependency-groups]
dev = ["pytest>=8"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/deploy"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

Create empty `src/deploy/__init__.py` and an empty `tests/__init__.py` — Task 12's
tests import helpers from `tests.test_commands`, which needs `tests` to be a
package. Then run `uv sync` to verify the project resolves.

- [ ] **Step 2: Write `src/deploy/paths.py`**

```python
from dataclasses import dataclass
from pathlib import Path

MANAGED_HEADER = "# Managed by deploy — edits will be overwritten"


@dataclass(frozen=True)
class Paths:
    """Every location the tool writes to. `under()` exists so tests and
    `--root` can redirect the whole layout into a temp directory."""

    apps: Path
    env: Path
    units: Path
    nginx: Path
    static: Path

    @classmethod
    def default(cls) -> "Paths":
        return cls(
            apps=Path.home() / "apps",
            env=Path("/etc/deploy/env"),
            units=Path("/etc/deploy/systemd"),
            nginx=Path("/etc/nginx/deploy.d"),
            static=Path("/var/www/deploy"),
        )

    @classmethod
    def under(cls, root: Path) -> "Paths":
        return cls(
            apps=root / "apps",
            env=root / "etc/deploy/env",
            units=root / "etc/deploy/systemd",
            nginx=root / "etc/nginx/deploy.d",
            static=root / "var/www/deploy",
        )

    def unit_file(self, name: str) -> Path:
        return self.units / f"{name}.service"

    def nginx_file(self, name: str) -> Path:
        return self.nginx / f"{name}.conf"

    def env_file(self, name: str) -> Path:
        return self.env / f"{name}.env"

    def clone_dir(self, name: str) -> Path:
        return self.apps / name
```

- [ ] **Step 3: Write the failing config tests**

`tests/test_config.py`:

```python
import pytest

from deploy.config import ConfigError, parse_config

SERVICE_TOML = """
[app]
name = "pokemon"

[build]
workdir = "pokemon-cards-app"
steps = ["npm install", "npm run build"]

[service]
workdir = "server"
start = "uv run uvicorn main:app --host 127.0.0.1 --port $PORT"
port = 8151

[nginx]
path = "/pokemon/"
strip_prefix = false
client_max_body_size = "10m"

[secrets]
COLLECTION_PASSWORD = "password for the collection upload endpoint"
"""

STATIC_TOML = """
[app]
name = "boggle"
type = "static"

[build]
steps = ["uv run python make_static.py"]
output = "static"

[nginx]
path = "/boggle/"
"""


def test_parses_a_service():
    c = parse_config(SERVICE_TOML, repo_name="pokemon")
    assert c.name == "pokemon"
    assert c.type == "service"
    assert c.service.start.endswith("--port $PORT")
    assert c.service.workdir == "server"
    assert c.service.port == 8151
    assert c.build.steps == ("npm install", "npm run build")
    assert c.build.workdir == "pokemon-cards-app"
    assert c.nginx.path == "/pokemon/"
    assert c.nginx.strip_prefix is False
    assert c.nginx.client_max_body_size == "10m"
    assert c.secrets == {"COLLECTION_PASSWORD": "password for the collection upload endpoint"}


def test_parses_a_static_app():
    c = parse_config(STATIC_TOML, repo_name="boggle-solver")
    assert c.name == "boggle"
    assert c.type == "static"
    assert c.service is None
    assert c.build.output == "static"
    assert c.nginx.path == "/boggle/"


def test_name_defaults_to_repo_name():
    c = parse_config('[service]\nstart = "run"\n', repo_name="crochet")
    assert c.name == "crochet"


def test_strip_prefix_defaults_to_true():
    c = parse_config(
        '[service]\nstart = "run"\n[nginx]\npath = "/x/"\n', repo_name="x"
    )
    assert c.nginx.strip_prefix is True


def test_absent_sections_are_none():
    c = parse_config('[service]\nstart = "run"\n', repo_name="x")
    assert c.build is None
    assert c.nginx is None
    assert c.dev_start is None
    assert c.env == {}
    assert c.secrets == {}


def test_dev_start_overrides():
    c = parse_config(
        '[service]\nstart = "prod"\n[dev]\nstart = "dev --reload"\n', repo_name="x"
    )
    assert c.service.start == "prod"
    assert c.dev_start == "dev --reload"


def test_service_requires_start():
    with pytest.raises(ConfigError, match="service.start"):
        parse_config('[service]\nworkdir = "x"\n', repo_name="x")


def test_service_type_requires_service_section():
    with pytest.raises(ConfigError, match="service"):
        parse_config('[app]\nname = "x"\n', repo_name="x")


def test_static_rejects_service_section():
    with pytest.raises(ConfigError, match="static"):
        parse_config(
            '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n'
            '[service]\nstart = "run"\n',
            repo_name="x",
        )


def test_static_rejects_secrets():
    with pytest.raises(ConfigError, match="secrets"):
        parse_config(
            '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n'
            '[secrets]\nA = "a"\n',
            repo_name="x",
        )


def test_static_rejects_strip_prefix():
    with pytest.raises(ConfigError, match="strip_prefix"):
        parse_config(
            '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n'
            '[nginx]\npath = "/x/"\nstrip_prefix = true\n',
            repo_name="x",
        )


def test_static_requires_build_output():
    with pytest.raises(ConfigError, match="build.output"):
        parse_config(
            '[app]\ntype = "static"\n[build]\nsteps = ["make"]\n', repo_name="x"
        )


def test_env_and_secrets_name_collision_is_an_error():
    with pytest.raises(ConfigError, match="both"):
        parse_config(
            '[service]\nstart = "run"\n[env]\nA = "1"\n[secrets]\nA = "desc"\n',
            repo_name="x",
        )


def test_nginx_path_must_be_absolute():
    with pytest.raises(ConfigError, match="must start with"):
        parse_config(
            '[service]\nstart = "run"\n[nginx]\npath = "pokemon/"\n', repo_name="x"
        )


def test_start_command_may_not_contain_a_single_quote():
    with pytest.raises(ConfigError, match="single quote"):
        parse_config("""[service]\nstart = "echo 'hi'"\n""", repo_name="x")


def test_unknown_app_type_is_rejected():
    with pytest.raises(ConfigError, match="type"):
        parse_config('[app]\ntype = "worker"\n', repo_name="x")


def test_invalid_toml_raises_config_error():
    with pytest.raises(ConfigError):
        parse_config("this is not toml {{{", repo_name="x")
```

- [ ] **Step 4: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'deploy.config'`

- [ ] **Step 5: Write `src/deploy/config.py`**

```python
import tomllib
from dataclasses import dataclass, field
from typing import Any

VALID_TYPES = ("service", "static")


class ConfigError(Exception):
    """A deploy.toml that cannot be used. The message is shown to the user."""


@dataclass(frozen=True)
class BuildConfig:
    steps: tuple[str, ...]
    workdir: str = ""
    output: str = ""


@dataclass(frozen=True)
class ServiceConfig:
    start: str
    workdir: str = ""
    health_path: str | None = None
    port: int | None = None


@dataclass(frozen=True)
class NginxConfig:
    path: str
    strip_prefix: bool = True
    client_max_body_size: str | None = None


@dataclass(frozen=True)
class AppConfig:
    name: str
    type: str
    build: BuildConfig | None = None
    service: ServiceConfig | None = None
    nginx: NginxConfig | None = None
    dev_start: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    secrets: dict[str, str] = field(default_factory=dict)

    @property
    def is_static(self) -> bool:
        return self.type == "static"


def parse_config(text: str, *, repo_name: str) -> AppConfig:
    try:
        raw: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"deploy.toml is not valid TOML: {exc}") from exc

    app = raw.get("app", {})
    app_type = app.get("type", "service")
    if app_type not in VALID_TYPES:
        raise ConfigError(
            f"app.type must be one of {', '.join(VALID_TYPES)}, got {app_type!r}"
        )

    name = app.get("name") or repo_name
    env = {str(k): str(v) for k, v in raw.get("env", {}).items()}
    secrets = {str(k): str(v) for k, v in raw.get("secrets", {}).items()}

    both = sorted(set(env) & set(secrets))
    if both:
        raise ConfigError(
            f"{', '.join(both)} appears in both [env] and [secrets]; pick one"
        )

    build = _parse_build(raw.get("build"))
    nginx = _parse_nginx(raw.get("nginx"), app_type)

    if app_type == "static":
        if "service" in raw:
            raise ConfigError("a static app must not declare [service]")
        if secrets:
            raise ConfigError("a static app must not declare [secrets]")
        if build is None or not build.output:
            raise ConfigError("a static app requires build.output")
        service = None
    else:
        service = _parse_service(raw.get("service"))

    dev = raw.get("dev", {})
    dev_start = dev.get("start")

    return AppConfig(
        name=name,
        type=app_type,
        build=build,
        service=service,
        nginx=nginx,
        dev_start=dev_start,
        env=env,
        secrets=secrets,
    )


def _parse_build(raw: dict[str, Any] | None) -> BuildConfig | None:
    if raw is None:
        return None
    steps = tuple(str(s) for s in raw.get("steps", []))
    return BuildConfig(
        steps=steps,
        workdir=str(raw.get("workdir", "")),
        output=str(raw.get("output", "")),
    )


def _parse_service(raw: dict[str, Any] | None) -> ServiceConfig:
    if raw is None:
        raise ConfigError("a service app requires a [service] section")
    start = raw.get("start")
    if not start:
        raise ConfigError("service.start is required")
    if "'" in start:
        raise ConfigError(
            "service.start may not contain a single quote; it is embedded in the "
            "unit's ExecStart as /bin/bash -c '...'"
        )
    port = raw.get("port")
    return ServiceConfig(
        start=str(start),
        workdir=str(raw.get("workdir", "")),
        health_path=raw.get("health_path"),
        port=int(port) if port is not None else None,
    )


def _parse_nginx(raw: dict[str, Any] | None, app_type: str) -> NginxConfig | None:
    if raw is None:
        return None
    path = str(raw.get("path", ""))
    if not path.startswith("/"):
        raise ConfigError(f"nginx.path must start with '/', got {path!r}")
    if app_type == "static" and "strip_prefix" in raw:
        raise ConfigError("strip_prefix is meaningless for a static app")
    size = raw.get("client_max_body_size")
    return NginxConfig(
        path=path,
        strip_prefix=bool(raw.get("strip_prefix", True)),
        client_max_body_size=str(size) if size is not None else None,
    )
```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS, 17 tests.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/deploy/__init__.py src/deploy/paths.py src/deploy/config.py tests/test_config.py
git commit -m "feat: deploy.toml parsing and validation"
```

---

### Task 2: nginx snippet rendering

**Files:**
- Create: `src/deploy/render.py`
- Test: `tests/test_render_nginx.py`

**Interfaces:**
- Consumes: `AppConfig`, `NginxConfig` from `deploy.config`; `Paths`, `MANAGED_HEADER` from `deploy.paths`.
- Produces: `render_nginx(config: AppConfig, port: int | None, paths: Paths) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/test_render_nginx.py`:

```python
from pathlib import Path

from deploy.config import parse_config
from deploy.paths import Paths
from deploy.render import render_nginx

PATHS = Paths.under(Path("/srv/test"))


def render(toml: str, port: int | None = 8201) -> str:
    return render_nginx(parse_config(toml, repo_name="x"), port, PATHS)


def test_service_without_strip_has_no_trailing_slash_on_proxy_pass():
    out = render(
        '[service]\nstart = "run"\n'
        '[nginx]\npath = "/pokemon/"\nstrip_prefix = false\n',
        port=8151,
    )
    assert "proxy_pass http://127.0.0.1:8151;" in out
    assert "proxy_pass http://127.0.0.1:8151/;" not in out


def test_service_with_strip_has_trailing_slash_on_proxy_pass():
    out = render('[service]\nstart = "run"\n[nginx]\npath = "/crochet/"\n', port=8152)
    assert "proxy_pass http://127.0.0.1:8152/;" in out


def test_trailing_slash_path_emits_the_redirect():
    out = render('[service]\nstart = "run"\n[nginx]\npath = "/pokemon/"\n')
    assert "location = /pokemon { return 301 /pokemon/; }" in out


def test_non_trailing_slash_path_emits_no_redirect():
    out = render('[service]\nstart = "run"\n[nginx]\npath = "/blog"\n')
    assert "return 301" not in out
    assert "location /blog {" in out


def test_forwarded_headers_are_always_present():
    out = render('[service]\nstart = "run"\n[nginx]\npath = "/blog"\n')
    assert "proxy_set_header Host $host;" in out
    assert "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;" in out
    assert "proxy_set_header X-Forwarded-Proto $scheme;" in out
    assert "proxy_set_header X-Forwarded-Prefix /blog;" in out


def test_client_max_body_size_is_emitted_only_when_set():
    with_size = render(
        '[service]\nstart = "run"\n'
        '[nginx]\npath = "/blog"\nclient_max_body_size = "10m"\n'
    )
    without = render('[service]\nstart = "run"\n[nginx]\npath = "/blog"\n')
    assert "client_max_body_size 10m;" in with_size
    assert "client_max_body_size" not in without


def test_static_app_uses_alias_and_try_files():
    out = render(
        '[app]\ntype = "static"\n[build]\nsteps = []\noutput = "static"\n'
        '[nginx]\npath = "/boggle/"\n',
        port=None,
    )
    assert "alias /srv/test/var/www/deploy/x/;" in out
    assert "try_files $uri $uri/ =404;" in out
    assert "proxy_pass" not in out
    assert "location = /boggle { return 301 /boggle/; }" in out


def test_every_snippet_starts_with_the_managed_header():
    out = render('[service]\nstart = "run"\n[nginx]\npath = "/blog"\n')
    assert out.startswith("# Managed by deploy — edits will be overwritten\n")


def test_snippet_ends_with_exactly_one_newline():
    out = render('[service]\nstart = "run"\n[nginx]\npath = "/blog"\n')
    assert out.endswith("}\n")
    assert not out.endswith("\n\n")
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_render_nginx.py -v`
Expected: FAIL — `No module named 'deploy.render'`

- [ ] **Step 3: Write `src/deploy/render.py`**

```python
from deploy.config import AppConfig
from deploy.paths import MANAGED_HEADER, Paths


def render_nginx(config: AppConfig, port: int | None, paths: Paths) -> str:
    """The location block(s) for one app. Pure: no IO, no subprocess."""
    if config.nginx is None:
        raise ValueError(f"{config.name} has no [nginx] section to render")

    path = config.nginx.path
    lines = [MANAGED_HEADER]

    # nginx treats /x and /x/ as different locations; a trailing-slash route
    # needs an explicit redirect or the bare URL 404s.
    if path.endswith("/"):
        bare = path.rstrip("/")
        lines.append(f"location = {bare} {{ return 301 {path}; }}")

    lines.append(f"location {path} {{")
    if config.is_static:
        lines.append(f"    alias {paths.static / config.name}/;")
        lines.append("    try_files $uri $uri/ =404;")
    else:
        if config.nginx.client_max_body_size:
            lines.append(
                f"    client_max_body_size {config.nginx.client_max_body_size};"
            )
        lines.append("    proxy_set_header Host $host;")
        lines.append(
            "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;"
        )
        lines.append("    proxy_set_header X-Forwarded-Proto $scheme;")
        lines.append(f"    proxy_set_header X-Forwarded-Prefix {path};")
        # The trailing slash is the whole of strip_prefix: with it nginx
        # replaces the matched prefix, without it the full URI is passed on.
        suffix = "/" if config.nginx.strip_prefix else ""
        lines.append(f"    proxy_pass http://127.0.0.1:{port}{suffix};")
    lines.append("}")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_render_nginx.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Compare against the live config by eye**

Render the pokemon case and diff it mentally against `examples/nginx` lines 43–50. The only intended differences are the managed header and `127.0.0.1` in place of the original's identical `127.0.0.1`. If `proxy_pass` gains or loses a trailing slash relative to the live file, the test above is wrong — fix the test, not the renderer.

- [ ] **Step 6: Commit**

```bash
git add src/deploy/render.py tests/test_render_nginx.py
git commit -m "feat: render nginx location blocks"
```

---

### Task 3: systemd unit rendering and the render aggregate

**Files:**
- Modify: `src/deploy/render.py`
- Test: `tests/test_render_unit.py`

**Interfaces:**
- Consumes: `render_nginx` from Task 2.
- Produces: `render_unit(config: AppConfig, port: int, paths: Paths) -> str` and `render(config: AppConfig, port: int | None, paths: Paths) -> dict[Path, str]` mapping absolute destination path to file contents.

- [ ] **Step 1: Write the failing tests**

`tests/test_render_unit.py`:

```python
from pathlib import Path

import pytest

from deploy.config import parse_config
from deploy.paths import Paths
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
    assert unit().startswith("# Managed by deploy — edits will be overwritten\n")


def test_workdir_is_the_clone_plus_service_workdir():
    assert "WorkingDirectory=/srv/test/apps/pokemon/server\n" in unit()


def test_workdir_omits_the_subdir_when_unset():
    out = unit('[service]\nstart = "run"\n')
    assert "WorkingDirectory=/srv/test/apps/pokemon\n" in out


def test_path_is_explicit_not_a_login_shell():
    out = unit()
    assert (
        "Environment=PATH=/home/nathan/.local/bin:/usr/local/bin:/usr/bin:/bin\n"
        in out
    )
    assert "bash -lc" not in out


def test_port_is_injected_as_an_environment_variable():
    assert "Environment=PORT=8151\n" in unit()


def test_non_secret_env_becomes_environment_lines():
    assert "Environment=LOG_LEVEL=info\n" in unit()


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


def test_restart_policy_matches_the_existing_hand_written_units():
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
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_render_unit.py -v`
Expected: FAIL — `cannot import name 'render_unit'`

- [ ] **Step 3: Append to `src/deploy/render.py`**

Add this import at the top of the file, alongside the existing ones:

```python
from pathlib import Path
```

Then append:

```python
UNIT_PATH = "/home/nathan/.local/bin:/usr/local/bin:/usr/bin:/bin"


def render_unit(config: AppConfig, port: int, paths: Paths) -> str:
    """The systemd unit for one service. Pure: no IO, no subprocess."""
    if config.is_static:
        raise ValueError(f"{config.name} is static and has no unit")
    if config.service is None:
        raise ValueError(f"{config.name} has no [service] section")

    workdir = paths.clone_dir(config.name)
    if config.service.workdir:
        workdir = workdir / config.service.workdir

    lines = [
        MANAGED_HEADER,
        "[Unit]",
        f"Description={config.name} (managed by deploy)",
        "After=network.target",
        "StartLimitIntervalSec=0",
        "",
        "[Service]",
        "Type=simple",
        "User=nathan",
        f"WorkingDirectory={workdir}",
        f"Environment=PATH={UNIT_PATH}",
        f"Environment=PORT={port}",
    ]
    for key in sorted(config.env):
        lines.append(f"Environment={key}={config.env[key]}")
    if config.secrets:
        lines.append(f"EnvironmentFile={paths.env_file(config.name)}")
    lines += [
        f"ExecStart=/bin/bash -c 'exec {config.service.start}'",
        "Restart=always",
        "RestartSec=1",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    return "\n".join(lines) + "\n"


def render(config: AppConfig, port: int | None, paths: Paths) -> dict[Path, str]:
    """Every file this app owns, as absolute path -> exact contents.

    This is the whole of the tool's desired state. Reconcile diffs it against
    disk; nothing else decides what gets written.
    """
    files: dict[Path, str] = {}
    if not config.is_static:
        if port is None:
            raise ValueError(f"{config.name} is a service and requires a port")
        files[paths.systemd_unit_file(config.name)] = render_unit(config, port, paths)
    if config.nginx is not None:
        files[paths.nginx_snippet_file(config.name)] = render_nginx(config, port, paths)
    return files
```

- [ ] **Step 4: Run the full test suite**

Run: `uv run pytest -v`
Expected: PASS, 41 tests (17 config + 9 nginx + 15 unit).

- [ ] **Step 5: Commit**

```bash
git add src/deploy/render.py tests/test_render_unit.py
git commit -m "feat: render systemd units and aggregate desired state"
```

---

### Task 4: Port allocation from existing units

**Files:**
- Create: `src/deploy/ports.py`
- Test: `tests/test_ports.py`

**Interfaces:**
- Consumes: `Paths` from `deploy.paths`.
- Produces: `PORT_RANGE: range`, `PortExhausted(Exception)`, `ports_in_use(paths: Paths, *, exclude: str | None = None) -> dict[str, int]`, `allocate_port(paths: Paths, *, name: str, pinned: int | None = None) -> int`.

- [ ] **Step 1: Write the failing tests**

`tests/test_ports.py`:

```python
import pytest

from deploy.paths import Paths
from deploy.ports import PortExhausted, allocate_port, ports_in_use


def write_unit(paths: Paths, name: str, port: int) -> None:
    paths.units.mkdir(parents=True, exist_ok=True)
    paths.systemd_unit_file(name).write_text(
        "# Managed by deploy — edits will be overwritten\n"
        "[Service]\n"
        f"Environment=PORT={port}\n"
    )


def test_no_units_means_nothing_in_use(tmp_path):
    assert ports_in_use(Paths.under(tmp_path)) == {}


def test_reads_ports_back_out_of_units(tmp_path):
    paths = Paths.under(tmp_path)
    write_unit(paths, "blog", 8080)
    write_unit(paths, "pokemon", 8151)
    assert ports_in_use(paths) == {"blog": 8080, "pokemon": 8151}


def test_exclude_hides_one_apps_own_port(tmp_path):
    paths = Paths.under(tmp_path)
    write_unit(paths, "blog", 8080)
    assert ports_in_use(paths, exclude="blog") == {}


def test_allocates_the_lowest_free_port_in_range(tmp_path):
    assert allocate_port(Paths.under(tmp_path), name="new") == 8200


def test_skips_ports_already_taken(tmp_path):
    paths = Paths.under(tmp_path)
    write_unit(paths, "a", 8200)
    write_unit(paths, "b", 8201)
    assert allocate_port(paths, name="new") == 8202


def test_ports_outside_the_range_do_not_consume_range_slots(tmp_path):
    paths = Paths.under(tmp_path)
    write_unit(paths, "blog", 8080)
    assert allocate_port(paths, name="new") == 8200


def test_a_pinned_port_is_returned_unchanged(tmp_path):
    assert allocate_port(Paths.under(tmp_path), name="blog", pinned=8080) == 8080


def test_a_pinned_port_that_collides_is_rejected(tmp_path):
    paths = Paths.under(tmp_path)
    write_unit(paths, "other", 8151)
    with pytest.raises(ValueError, match="8151"):
        allocate_port(paths, name="pokemon", pinned=8151)


def test_an_app_may_keep_its_own_pinned_port_on_update(tmp_path):
    paths = Paths.under(tmp_path)
    write_unit(paths, "pokemon", 8151)
    assert allocate_port(paths, name="pokemon", pinned=8151) == 8151


def test_reallocating_an_existing_app_keeps_its_port(tmp_path):
    paths = Paths.under(tmp_path)
    write_unit(paths, "app", 8205)
    assert allocate_port(paths, name="app") == 8205


def test_exhausted_range_raises(tmp_path):
    paths = Paths.under(tmp_path)
    for i, port in enumerate(range(8200, 8300)):
        write_unit(paths, f"app{i}", port)
    with pytest.raises(PortExhausted):
        allocate_port(paths, name="new")
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_ports.py -v`
Expected: FAIL — `No module named 'deploy.ports'`

- [ ] **Step 3: Write `src/deploy/ports.py`**

```python
import re

from deploy.paths import Paths

PORT_RANGE = range(8200, 8300)
_PORT_LINE = re.compile(r"^Environment=PORT=(\d+)$", re.MULTILINE)


class PortExhausted(Exception):
    """Every port in PORT_RANGE is taken."""


def ports_in_use(paths: Paths, *, exclude: str | None = None) -> dict[str, int]:
    """App name -> port, read back out of the generated units.

    The units are the only record of port assignments; there is no state file
    that could disagree with them.
    """
    found: dict[str, int] = {}
    if not paths.units.is_dir():
        return found
    for unit in sorted(paths.units.glob("*.service")):
        name = unit.stem
        if name == exclude:
            continue
        match = _PORT_LINE.search(unit.read_text())
        if match:
            found[name] = int(match.group(1))
    return found


def allocate_port(paths: Paths, *, name: str, pinned: int | None = None) -> int:
    """Pick this app's port. An app always keeps the port it already has."""
    taken = ports_in_use(paths, exclude=name)
    mine = ports_in_use(paths).get(name)

    if pinned is not None:
        if pinned in taken.values():
            owner = next(n for n, p in taken.items() if p == pinned)
            raise ValueError(f"port {pinned} is already used by {owner}")
        return pinned

    if mine is not None:
        return mine

    used = set(taken.values())
    for port in PORT_RANGE:
        if port not in used:
            return port
    raise PortExhausted(
        f"no free port in {PORT_RANGE.start}-{PORT_RANGE.stop - 1}"
    )
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_ports.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Commit**

```bash
git add src/deploy/ports.py tests/test_ports.py
git commit -m "feat: allocate ports by reading existing units"
```

---

### Task 5: Command runner and reconcile

**Files:**
- Create: `src/deploy/runner.py`
- Create: `src/deploy/reconcile.py`
- Test: `tests/test_reconcile.py`

**Interfaces:**
- Consumes: `Paths`, `MANAGED_HEADER`.
- Produces: `Runner` protocol with `run(argv: Sequence[str], *, cwd: Path | None = None, env: dict | None = None, check: bool = True) -> subprocess.CompletedProcess`; `RealRunner`; `RecordingRunner(results: dict[str, int] | None = None)` exposing `.calls: list[list[str]]`. From reconcile: `Change(path: Path, before: str | None, after: str | None)`, `ForeignFile(Exception)`, `NginxTestFailed(Exception)`, `plan_changes(desired: dict[Path, str], *, remove: Iterable[Path] = ()) -> list[Change]`, `apply_changes(changes: list[Change], *, runner: Runner, dry_run: bool = False) -> set[str]` returning the set of reload actions performed (`{"daemon-reload", "nginx"}`).

- [ ] **Step 1: Write `src/deploy/runner.py`**

```python
import subprocess
from pathlib import Path
from typing import Protocol, Sequence


class Runner(Protocol):
    """Every subprocess the tool makes goes through this, so tests can record
    commands instead of running them."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess: ...


class RealRunner:
    def run(self, argv, *, cwd=None, env=None, check=True):
        return subprocess.run(
            list(argv),
            cwd=cwd,
            env=env,
            check=check,
            capture_output=True,
            text=True,
        )


class RecordingRunner:
    """Test double. `results` maps a substring of the joined command to the exit
    code it should return; `stdout` maps a substring to the output it should
    produce. Anything unmatched succeeds with empty output."""

    def __init__(
        self,
        results: dict[str, int] | None = None,
        stdout: dict[str, str] | None = None,
    ):
        self.calls: list[list[str]] = []
        self.results = results or {}
        self.stdout = stdout or {}

    def run(self, argv, *, cwd=None, env=None, check=True):
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        code = next(
            (c for frag, c in self.results.items() if frag in joined), 0
        )
        out = next((o for frag, o in self.stdout.items() if frag in joined), "")
        result = subprocess.CompletedProcess(argv, code, stdout=out, stderr="")
        if check and code != 0:
            raise subprocess.CalledProcessError(code, argv, output="", stderr="")
        return result

    def ran(self, fragment: str) -> bool:
        return any(fragment in " ".join(c) for c in self.calls)
```

- [ ] **Step 2: Write the failing reconcile tests**

`tests/test_reconcile.py`:

```python
import pytest

from deploy.paths import MANAGED_HEADER, Paths
from deploy.reconcile import (
    ForeignFile,
    NginxTestFailed,
    apply_changes,
    plan_changes,
)
from deploy.runner import RecordingRunner

UNIT = MANAGED_HEADER + "\n[Service]\nEnvironment=PORT=8200\n"
CONF = MANAGED_HEADER + "\nlocation /x/ { proxy_pass http://127.0.0.1:8200/; }\n"


def desired(paths: Paths) -> dict:
    return {paths.systemd_unit_file("x"): UNIT, paths.nginx_snippet_file("x"): CONF}


def test_everything_is_a_change_when_nothing_exists(tmp_path):
    paths = Paths.under(tmp_path)
    changes = plan_changes(desired(paths))
    assert len(changes) == 2
    assert all(c.before is None for c in changes)


def test_applying_changes_writes_the_files(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    assert paths.systemd_unit_file("x").read_text() == UNIT
    assert paths.nginx_snippet_file("x").read_text() == CONF


def test_a_second_identical_run_plans_nothing(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    assert plan_changes(desired(paths)) == []


def test_a_second_identical_run_reloads_nothing(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    runner = RecordingRunner()
    actions = apply_changes(plan_changes(desired(paths)), runner=runner)
    assert actions == set()
    assert runner.calls == []


def test_changing_only_the_unit_does_not_reload_nginx(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    changed = dict(desired(paths))
    changed[paths.systemd_unit_file("x")] = UNIT.replace("8200", "8201")
    runner = RecordingRunner()
    actions = apply_changes(plan_changes(changed), runner=runner)
    assert actions == {"daemon-reload"}
    assert not runner.ran("nginx")


def test_changing_only_nginx_does_not_daemon_reload(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    changed = dict(desired(paths))
    changed[paths.nginx_snippet_file("x")] = CONF.replace("8200", "8201")
    runner = RecordingRunner()
    actions = apply_changes(plan_changes(changed), runner=runner)
    assert actions == {"nginx"}
    assert not runner.ran("daemon-reload")


def test_nginx_is_tested_before_it_is_reloaded(tmp_path):
    paths = Paths.under(tmp_path)
    runner = RecordingRunner()
    apply_changes(plan_changes(desired(paths)), runner=runner)
    joined = [" ".join(c) for c in runner.calls]
    assert any("nginx -t" in c for c in joined)
    assert joined.index(next(c for c in joined if "nginx -t" in c)) < joined.index(
        next(c for c in joined if "reload nginx" in c)
    )


def test_a_failed_nginx_test_restores_the_previous_content(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    broken = dict(desired(paths))
    broken[paths.nginx_snippet_file("x")] = MANAGED_HEADER + "\nthis is not nginx\n"
    runner = RecordingRunner(results={"nginx -t": 1})
    with pytest.raises(NginxTestFailed):
        apply_changes(plan_changes(broken), runner=runner)
    assert paths.nginx_snippet_file("x").read_text() == CONF


def test_a_failed_nginx_test_does_not_reload(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    broken = dict(desired(paths))
    broken[paths.nginx_snippet_file("x")] = MANAGED_HEADER + "\nbroken\n"
    runner = RecordingRunner(results={"nginx -t": 1})
    with pytest.raises(NginxTestFailed):
        apply_changes(plan_changes(broken), runner=runner)
    assert not runner.ran("reload nginx")


def test_a_file_without_the_managed_header_is_never_overwritten(tmp_path):
    paths = Paths.under(tmp_path)
    paths.units.mkdir(parents=True)
    paths.systemd_unit_file("x").write_text("[Service]\nExecStart=/hand/written\n")
    with pytest.raises(ForeignFile, match="x.service"):
        plan_changes(desired(paths))


def test_removal_is_planned_as_a_change_to_none(tmp_path):
    paths = Paths.under(tmp_path)
    apply_changes(plan_changes(desired(paths)), runner=RecordingRunner())
    changes = plan_changes({}, remove=[paths.systemd_unit_file("x"), paths.nginx_snippet_file("x")])
    assert {c.after for c in changes} == {None}
    apply_changes(changes, runner=RecordingRunner())
    assert not paths.systemd_unit_file("x").exists()


def test_dry_run_writes_nothing_and_runs_nothing(tmp_path):
    paths = Paths.under(tmp_path)
    runner = RecordingRunner()
    apply_changes(plan_changes(desired(paths)), runner=runner, dry_run=True)
    assert not paths.systemd_unit_file("x").exists()
    assert runner.calls == []
```

- [ ] **Step 3: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: FAIL — `No module named 'deploy.reconcile'`

- [ ] **Step 4: Write `src/deploy/reconcile.py`**

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from deploy.paths import MANAGED_HEADER
from deploy.runner import Runner

SYSTEMCTL = "/usr/bin/systemctl"


class ForeignFile(Exception):
    """A file we were about to overwrite was not written by us."""


class NginxTestFailed(Exception):
    """`nginx -t` rejected the new configuration; nothing was reloaded."""


@dataclass(frozen=True)
class Change:
    path: Path
    before: str | None
    after: str | None

    @property
    def is_removal(self) -> bool:
        return self.after is None


def plan_changes(
    desired: dict[Path, str], *, remove: Iterable[Path] = ()
) -> list[Change]:
    """Diff desired state against disk. Writes nothing.

    Raises ForeignFile rather than clobbering anything we did not generate.
    """
    changes: list[Change] = []
    for path, after in sorted(desired.items()):
        before = path.read_text() if path.exists() else None
        if before is not None and not before.startswith(MANAGED_HEADER):
            raise ForeignFile(
                f"{path} was not generated by deploy (no managed header); "
                "move it aside if you want deploy to own it"
            )
        if before != after:
            changes.append(Change(path, before, after))
    for path in sorted(remove):
        if path.exists():
            before = path.read_text()
            if not before.startswith(MANAGED_HEADER):
                raise ForeignFile(f"{path} was not generated by deploy")
            changes.append(Change(path, before, None))
    return changes


def apply_changes(
    changes: list[Change], *, runner: Runner, dry_run: bool = False
) -> set[str]:
    """Write the changes and perform only the reloads they require.

    Returns the set of reload actions performed, for the caller to report.
    """
    if not changes or dry_run:
        return set()

    touched_units = any("systemd" in str(c.path) for c in changes)
    touched_nginx = any("nginx" in str(c.path) for c in changes)

    for change in changes:
        if change.is_removal:
            change.path.unlink()
        else:
            change.path.parent.mkdir(parents=True, exist_ok=True)
            change.path.write_text(change.after)

    actions: set[str] = set()

    if touched_nginx:
        # Validate before reloading; on failure put back exactly what was there
        # so a broken render cannot take the site down.
        result = runner.run(["sudo", "nginx", "-t"], check=False)
        if result.returncode != 0:
            _rollback([c for c in changes if "nginx" in str(c.path)])
            raise NginxTestFailed(result.stderr or "nginx -t failed")
        runner.run(["sudo", SYSTEMCTL, "reload", "nginx"])
        actions.add("nginx")

    if touched_units:
        runner.run(["sudo", SYSTEMCTL, "daemon-reload"])
        actions.add("daemon-reload")

    return actions


def _rollback(changes: list[Change]) -> None:
    for change in changes:
        if change.before is None:
            change.path.unlink(missing_ok=True)
        else:
            change.path.write_text(change.before)
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_reconcile.py -v`
Expected: PASS, 12 tests.

- [ ] **Step 6: Commit**

```bash
git add src/deploy/runner.py src/deploy/reconcile.py tests/test_reconcile.py
git commit -m "feat: reconcile desired state against disk"
```

---

### Task 6: Static publishing with an atomic symlink swap

**Files:**
- Create: `src/deploy/static.py`
- Test: `tests/test_static.py`

**Interfaces:**
- Consumes: `Paths`.
- Produces: `publish(source: Path, *, name: str, commit: str, paths: Paths) -> Path` returning the versioned directory now pointed at; `live_target(name: str, paths: Paths) -> Path | None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_static.py`:

```python
import pytest

from deploy.paths import Paths
from deploy.static import live_target, publish


def build(tmp_path, content: str):
    src = tmp_path / "build-output"
    src.mkdir(exist_ok=True)
    (src / "index.html").write_text(content)
    return src


def test_publish_creates_a_versioned_directory(tmp_path):
    paths = Paths.under(tmp_path)
    target = publish(build(tmp_path, "v1"), name="boggle", commit="abc123", paths=paths)
    assert target.name == "boggle-abc123"
    assert (target / "index.html").read_text() == "v1"


def test_publish_points_the_live_symlink_at_the_new_build(tmp_path):
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="abc123", paths=paths)
    live = paths.static / "boggle"
    assert live.is_symlink()
    assert (live / "index.html").read_text() == "v1"


def test_publishing_again_swaps_the_symlink(tmp_path):
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="aaa", paths=paths)
    publish(build(tmp_path, "v2"), name="boggle", commit="bbb", paths=paths)
    assert (paths.static / "boggle" / "index.html").read_text() == "v2"


def test_the_previous_build_is_retained_for_rollback(tmp_path):
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="aaa", paths=paths)
    publish(build(tmp_path, "v2"), name="boggle", commit="bbb", paths=paths)
    assert (paths.static / "boggle-aaa" / "index.html").read_text() == "v1"


def test_live_target_reports_the_current_commit(tmp_path):
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="aaa", paths=paths)
    assert live_target("boggle", paths).name == "boggle-aaa"


def test_live_target_is_none_when_nothing_is_published(tmp_path):
    assert live_target("boggle", Paths.under(tmp_path)) is None


def test_republishing_the_same_commit_replaces_the_directory(tmp_path):
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="aaa", paths=paths)
    publish(build(tmp_path, "v1-rebuilt"), name="boggle", commit="aaa", paths=paths)
    assert (paths.static / "boggle" / "index.html").read_text() == "v1-rebuilt"


def test_a_missing_build_output_is_an_error_and_leaves_live_alone(tmp_path):
    paths = Paths.under(tmp_path)
    publish(build(tmp_path, "v1"), name="boggle", commit="aaa", paths=paths)
    with pytest.raises(FileNotFoundError):
        publish(tmp_path / "nope", name="boggle", commit="bbb", paths=paths)
    assert (paths.static / "boggle" / "index.html").read_text() == "v1"
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_static.py -v`
Expected: FAIL — `No module named 'deploy.static'`

- [ ] **Step 3: Write `src/deploy/static.py`**

```python
import os
import shutil
from pathlib import Path

from deploy.paths import Paths


def publish(source: Path, *, name: str, commit: str, paths: Paths) -> Path:
    """Copy a build output into place and swap the live symlink onto it.

    The swap is a rename, so a reader never sees a half-copied tree: it gets
    either the whole old build or the whole new one.
    """
    if not source.is_dir():
        raise FileNotFoundError(f"build output {source} does not exist")

    paths.static.mkdir(parents=True, exist_ok=True)
    target = paths.static / f"{name}-{commit}"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)

    # Create the new link under a temp name, then rename it over the old one.
    # os.replace on a symlink is atomic; writing the link in place is not.
    tmp_link = paths.static / f".{name}.swap"
    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink()
    tmp_link.symlink_to(target.name)
    os.replace(tmp_link, paths.static / name)
    return target


def live_target(name: str, paths: Paths) -> Path | None:
    """The versioned directory the live symlink points at, if any."""
    link = paths.static / name
    if not link.is_symlink():
        return None
    return paths.static / os.readlink(link)
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_static.py -v`
Expected: PASS, 8 tests.

- [ ] **Step 5: Commit**

```bash
git add src/deploy/static.py tests/test_static.py
git commit -m "feat: publish static builds via atomic symlink swap"
```

---

### Task 7: Secrets and environment files

**Files:**
- Create: `src/deploy/secrets.py`
- Test: `tests/test_secrets.py`

**Interfaces:**
- Consumes: `Paths`.
- Produces: `read_env_file(path: Path) -> dict[str, str]`, `missing_secrets(declared: dict[str, str], existing: dict[str, str]) -> list[str]`, `write_env_file(path: Path, values: dict[str, str]) -> None`, `merge_secrets(path: Path, new: dict[str, str]) -> None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_secrets.py`:

```python
import stat

from deploy.secrets import (
    merge_secrets,
    missing_secrets,
    read_env_file,
    write_env_file,
)


def test_reading_a_missing_file_gives_an_empty_mapping(tmp_path):
    assert read_env_file(tmp_path / "nope.env") == {}


def test_round_trips_values(tmp_path):
    path = tmp_path / "a.env"
    write_env_file(path, {"A": "1", "B": "two"})
    assert read_env_file(path) == {"A": "1", "B": "two"}


def test_env_file_is_owner_read_write_only(tmp_path):
    path = tmp_path / "a.env"
    write_env_file(path, {"SECRET": "hunter2"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_values_containing_spaces_survive(tmp_path):
    path = tmp_path / "a.env"
    write_env_file(path, {"MSG": "hello there"})
    assert read_env_file(path)["MSG"] == "hello there"


def test_comments_and_blank_lines_are_ignored_on_read(tmp_path):
    path = tmp_path / "a.env"
    path.write_text("# a comment\n\nA=1\n")
    assert read_env_file(path) == {"A": "1"}


def test_missing_secrets_lists_only_the_absent_ones(tmp_path):
    declared = {"A": "desc a", "B": "desc b"}
    assert missing_secrets(declared, {"A": "set"}) == ["B"]


def test_nothing_missing_when_all_are_present():
    assert missing_secrets({"A": "d"}, {"A": "v"}) == []


def test_merge_adds_without_dropping_existing_values(tmp_path):
    path = tmp_path / "a.env"
    write_env_file(path, {"OLD": "keep"})
    merge_secrets(path, {"NEW": "added"})
    assert read_env_file(path) == {"OLD": "keep", "NEW": "added"}


def test_merge_keeps_permissions_tight(tmp_path):
    path = tmp_path / "a.env"
    write_env_file(path, {"OLD": "keep"})
    merge_secrets(path, {"NEW": "added"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_secrets.py -v`
Expected: FAIL — `No module named 'deploy.secrets'`

- [ ] **Step 3: Write `src/deploy/secrets.py`**

```python
import os
from pathlib import Path

# systemd's EnvironmentFile parser takes bare KEY=VALUE lines and does not
# strip quotes the way a shell would, so values are written unquoted.


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value
    return values


def write_env_file(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}={values[k]}\n" for k in sorted(values))
    path.write_text(body)
    os.chmod(path, 0o600)


def merge_secrets(path: Path, new: dict[str, str]) -> None:
    merged = read_env_file(path)
    merged.update(new)
    write_env_file(path, merged)


def missing_secrets(declared: dict[str, str], existing: dict[str, str]) -> list[str]:
    """Declared secret names with no value on this machine yet."""
    return [name for name in sorted(declared) if name not in existing]
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_secrets.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add src/deploy/secrets.py tests/test_secrets.py
git commit -m "feat: secret env files at mode 0600"
```

---

### Task 8: Git operations

**Files:**
- Create: `src/deploy/gitrepo.py`
- Test: `tests/test_gitrepo.py`

**Interfaces:**
- Consumes: `Runner` from `deploy.runner`.
- Produces: `repo_url(name_or_url: str) -> str`, `clone(url: str, dest: Path, *, runner: Runner) -> None`, `remote_url(repo: Path, *, runner: Runner) -> str`, `head_commit(repo: Path, *, runner: Runner) -> str`, `is_dirty(repo: Path, *, runner: Runner) -> bool`, `pull_ff_only(repo: Path, *, runner: Runner) -> bool` returning whether new commits arrived, `DirtyRepo(Exception)`.

- [ ] **Step 1: Write the failing tests**

These use real temporary git repositories with `RealRunner` — git is fast and the behaviour being tested (fast-forward refusal, dirty detection) is exactly what a mock would get wrong.

`tests/test_gitrepo.py`:

```python
import subprocess

import pytest

from deploy.gitrepo import (
    DirtyRepo,
    clone,
    head_commit,
    is_dirty,
    pull_ff_only,
    remote_url,
    repo_url,
)
from deploy.runner import RealRunner

RUNNER = RealRunner()


def git(repo, *args):
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "PATH": "/usr/bin:/bin",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        },
    )


def make_origin(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    git(origin, "init", "-q")
    (origin / "README.md").write_text("one\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "one")
    return origin


def test_short_name_expands_to_the_github_account():
    assert repo_url("pokemon") == "git@github.com:gnatpat/pokemon.git"


def test_a_full_url_is_passed_through():
    url = "git@github.com:someone/else.git"
    assert repo_url(url) == url


def test_an_https_url_is_passed_through():
    url = "https://github.com/someone/else.git"
    assert repo_url(url) == url


def test_clone_then_read_back_the_remote(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "work"
    clone(str(origin), dest, runner=RUNNER)
    assert remote_url(dest, runner=RUNNER) == str(origin)


def test_head_commit_is_a_full_sha(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "work"
    clone(str(origin), dest, runner=RUNNER)
    sha = head_commit(dest, runner=RUNNER)
    assert len(sha) == 40


def test_a_clean_clone_is_not_dirty(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "work"
    clone(str(origin), dest, runner=RUNNER)
    assert is_dirty(dest, runner=RUNNER) is False


def test_an_edited_file_makes_the_repo_dirty(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "work"
    clone(str(origin), dest, runner=RUNNER)
    (dest / "README.md").write_text("edited\n")
    assert is_dirty(dest, runner=RUNNER) is True


def test_pull_reports_false_when_there_is_nothing_new(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "work"
    clone(str(origin), dest, runner=RUNNER)
    assert pull_ff_only(dest, runner=RUNNER) is False


def test_pull_reports_true_and_advances_when_upstream_moves(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "work"
    clone(str(origin), dest, runner=RUNNER)
    before = head_commit(dest, runner=RUNNER)
    (origin / "README.md").write_text("two\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "two")
    assert pull_ff_only(dest, runner=RUNNER) is True
    assert head_commit(dest, runner=RUNNER) != before


def test_pull_refuses_to_touch_a_dirty_tree(tmp_path):
    origin = make_origin(tmp_path)
    dest = tmp_path / "work"
    clone(str(origin), dest, runner=RUNNER)
    (dest / "README.md").write_text("local edit\n")
    with pytest.raises(DirtyRepo):
        pull_ff_only(dest, runner=RUNNER)
    assert (dest / "README.md").read_text() == "local edit\n"
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_gitrepo.py -v`
Expected: FAIL — `No module named 'deploy.gitrepo'`

- [ ] **Step 3: Write `src/deploy/gitrepo.py`**

```python
import os
from pathlib import Path

from deploy.runner import Runner

GITHUB_USER = os.environ.get("DEPLOY_GITHUB_USER", "gnatpat")


class DirtyRepo(Exception):
    """The working tree has local changes; deploy will not touch it."""


def repo_url(name_or_url: str) -> str:
    """A bare name means the owner's GitHub account; anything with a scheme or
    a colon is already a URL."""
    if "://" in name_or_url or ":" in name_or_url or "/" in name_or_url:
        return name_or_url
    return f"git@github.com:{GITHUB_USER}/{name_or_url}.git"


def clone(url: str, dest: Path, *, runner: Runner) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    runner.run(["git", "clone", url, str(dest)])


def remote_url(repo: Path, *, runner: Runner) -> str:
    return runner.run(
        ["git", "-C", str(repo), "remote", "get-url", "origin"]
    ).stdout.strip()


def head_commit(repo: Path, *, runner: Runner) -> str:
    return runner.run(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()


def is_dirty(repo: Path, *, runner: Runner) -> bool:
    out = runner.run(["git", "-C", str(repo), "status", "--porcelain"]).stdout
    return bool(out.strip())


def pull_ff_only(repo: Path, *, runner: Runner) -> bool:
    """Fast-forward to origin. Returns True if new commits arrived.

    Refuses a dirty tree rather than risking someone's uncommitted work, and
    refuses a non-fast-forward rather than creating a merge nobody asked for.
    """
    if is_dirty(repo, runner=runner):
        raise DirtyRepo(f"{repo} has uncommitted changes; commit or stash first")
    before = head_commit(repo, runner=runner)
    runner.run(["git", "-C", str(repo), "pull", "--ff-only"])
    return head_commit(repo, runner=runner) != before
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_gitrepo.py -v`
Expected: PASS, 10 tests.

If `test_clone_then_read_back_the_remote` fails because git demands a default branch name, add `-c init.defaultBranch=main` to the `git init` call in the test helper.

- [ ] **Step 5: Commit**

```bash
git add src/deploy/gitrepo.py tests/test_gitrepo.py
git commit -m "feat: git clone, fast-forward pull, and repo inspection"
```

---

### Task 9: Local prefix proxy

**Files:**
- Create: `src/deploy/proxy.py`
- Test: `tests/test_proxy.py`

**Interfaces:**
- Consumes: `NginxConfig` from `deploy.config`.
- Produces: `Decision` variants `Redirect(location: str)`, `Forward(path: str)`, `NotFound()`; `route_request(nginx: NginxConfig, request_path: str) -> Decision`; `forwarded_headers(nginx: NginxConfig, host: str, client_ip: str) -> dict[str, str]`; `serve(nginx: NginxConfig, *, upstream_port: int, listen_port: int) -> None` (blocking).

- [ ] **Step 1: Write the failing tests**

`tests/test_proxy.py`:

```python
from deploy.config import NginxConfig
from deploy.proxy import Forward, NotFound, Redirect, forwarded_headers, route_request


def test_bare_route_redirects_to_the_trailing_slash():
    nginx = NginxConfig(path="/pokemon/")
    assert route_request(nginx, "/pokemon") == Redirect("/pokemon/")


def test_a_non_trailing_slash_route_has_no_redirect():
    nginx = NginxConfig(path="/blog")
    assert route_request(nginx, "/blog") == Forward("/")


def test_strip_prefix_removes_the_route_from_the_upstream_path():
    nginx = NginxConfig(path="/crochet/", strip_prefix=True)
    assert route_request(nginx, "/crochet/rows") == Forward("/rows")


def test_no_strip_passes_the_whole_uri_through():
    nginx = NginxConfig(path="/pokemon/", strip_prefix=False)
    assert route_request(nginx, "/pokemon/cards") == Forward("/pokemon/cards")


def test_the_route_root_becomes_slash_when_stripping():
    nginx = NginxConfig(path="/crochet/", strip_prefix=True)
    assert route_request(nginx, "/crochet/") == Forward("/")


def test_query_strings_are_preserved():
    nginx = NginxConfig(path="/crochet/", strip_prefix=True)
    assert route_request(nginx, "/crochet/rows?a=1") == Forward("/rows?a=1")


def test_a_path_outside_the_route_is_not_found():
    nginx = NginxConfig(path="/crochet/")
    assert route_request(nginx, "/other") == NotFound()


def test_a_non_trailing_route_reproduces_nginx_double_slash():
    # nginx replaces the matched location prefix with the proxy_pass URI, so
    # `location /blog` + `proxy_pass .../` turns /blog/post into //post. This
    # is real nginx behaviour and a reason to prefer trailing-slash routes.
    nginx = NginxConfig(path="/blog", strip_prefix=True)
    assert route_request(nginx, "/blog/post") == Forward("//post")


def test_forwarded_headers_match_what_the_nginx_snippet_sets():
    nginx = NginxConfig(path="/pokemon/")
    headers = forwarded_headers(nginx, host="localhost:8000", client_ip="127.0.0.1")
    assert headers == {
        "Host": "localhost:8000",
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Proto": "http",
        "X-Forwarded-Prefix": "/pokemon/",
    }
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_proxy.py -v`
Expected: FAIL — `No module named 'deploy.proxy'`

- [ ] **Step 3: Write `src/deploy/proxy.py`**

```python
import http.client
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from deploy.config import NginxConfig


@dataclass(frozen=True)
class Redirect:
    location: str


@dataclass(frozen=True)
class Forward:
    path: str


@dataclass(frozen=True)
class NotFound:
    pass


Decision = Redirect | Forward | NotFound


def route_request(nginx: NginxConfig, request_path: str) -> Decision:
    """Decide what nginx would do with this path, given the same config that
    generates the production snippet."""
    route = nginx.path

    if route.endswith("/") and request_path == route.rstrip("/"):
        return Redirect(route)

    if not request_path.startswith(route):
        return NotFound()

    if not nginx.strip_prefix:
        return Forward(request_path)

    # nginx replaces the matched prefix with the proxy_pass URI, which is "/".
    return Forward("/" + request_path[len(route) :])


def forwarded_headers(
    nginx: NginxConfig, host: str, client_ip: str
) -> dict[str, str]:
    return {
        "Host": host,
        "X-Forwarded-For": client_ip,
        "X-Forwarded-Proto": "http",
        "X-Forwarded-Prefix": nginx.path,
    }


def _handler(nginx: NginxConfig, upstream_port: int) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _proxy(self) -> None:
            decision = route_request(nginx, self.path)

            if isinstance(decision, Redirect):
                self.send_response(301)
                self.send_header("Location", decision.location)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if isinstance(decision, NotFound):
                self.send_error(404)
                return

            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None

            conn = http.client.HTTPConnection("127.0.0.1", upstream_port)
            headers = {
                k: v
                for k, v in self.headers.items()
                if k.lower() not in ("host", "connection")
            }
            headers.update(
                forwarded_headers(
                    nginx, self.headers.get("Host", ""), self.client_address[0]
                )
            )
            try:
                conn.request(self.command, decision.path, body=body, headers=headers)
                upstream = conn.getresponse()
                payload = upstream.read()
            except OSError as exc:
                self.send_error(502, f"upstream unreachable: {exc}")
                return

            self.send_response(upstream.status)
            for key, value in upstream.getheaders():
                if key.lower() in ("transfer-encoding", "connection", "content-length"):
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _proxy
        do_POST = _proxy
        do_PUT = _proxy
        do_DELETE = _proxy
        do_PATCH = _proxy
        do_HEAD = _proxy

        def log_message(self, fmt, *args):
            print(f"[proxy] {fmt % args}")

    return Handler


def serve(nginx: NginxConfig, *, upstream_port: int, listen_port: int) -> None:
    server = ThreadingHTTPServer(
        ("127.0.0.1", listen_port), _handler(nginx, upstream_port)
    )
    print(
        f"[proxy] http://127.0.0.1:{listen_port}{nginx.path} "
        f"-> 127.0.0.1:{upstream_port} (strip_prefix={nginx.strip_prefix})"
    )
    server.serve_forever()
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_proxy.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Add an end-to-end proxy test against a stub upstream**

Append to `tests/test_proxy.py`:

```python
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from deploy.proxy import serve


class Echo(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        payload = json.dumps(
            {"path": self.path, "headers": dict(self.headers)}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def _start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]


def test_end_to_end_forwards_path_and_headers():
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    upstream_port = _start(upstream)

    nginx = NginxConfig(path="/pokemon/", strip_prefix=False)
    proxy = ThreadingHTTPServer(
        ("127.0.0.1", 0), __import__("deploy.proxy", fromlist=["_handler"])._handler(
            nginx, upstream_port
        )
    )
    proxy_port = _start(proxy)

    with urllib.request.urlopen(
        f"http://127.0.0.1:{proxy_port}/pokemon/cards"
    ) as response:
        seen = json.loads(response.read())

    assert seen["path"] == "/pokemon/cards"
    assert seen["headers"]["X-Forwarded-Prefix"] == "/pokemon/"
    assert seen["headers"]["X-Forwarded-Proto"] == "http"

    upstream.shutdown()
    proxy.shutdown()


def test_end_to_end_strips_the_prefix_when_configured():
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Echo)
    upstream_port = _start(upstream)

    nginx = NginxConfig(path="/crochet/", strip_prefix=True)
    proxy = ThreadingHTTPServer(
        ("127.0.0.1", 0), __import__("deploy.proxy", fromlist=["_handler"])._handler(
            nginx, upstream_port
        )
    )
    proxy_port = _start(proxy)

    with urllib.request.urlopen(
        f"http://127.0.0.1:{proxy_port}/crochet/rows"
    ) as response:
        seen = json.loads(response.read())

    assert seen["path"] == "/rows"

    upstream.shutdown()
    proxy.shutdown()
```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_proxy.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 7: Commit**

```bash
git add src/deploy/proxy.py tests/test_proxy.py
git commit -m "feat: local reverse proxy reproducing nginx prefix behaviour"
```

---

### Task 10: `deploy dev`

**Files:**
- Create: `src/deploy/dev.py`
- Test: `tests/test_dev.py`

**Interfaces:**
- Consumes: `AppConfig`, `read_env_file`, `missing_secrets`, `proxy.serve`, `Runner`.
- Produces: `MissingSecrets(Exception)` with `.names: list[str]`; `resolve_env(config: AppConfig, dotenv: dict[str, str], *, port: int) -> dict[str, str]`; `dev_command(config: AppConfig) -> str`; `dev_workdir(config: AppConfig, repo: Path) -> Path`; `run_dev(config: AppConfig, repo: Path, *, port: int, build: bool, prefix: bool, runner: Runner) -> int`.

- [ ] **Step 1: Write the failing tests**

`tests/test_dev.py`:

```python
from pathlib import Path

import pytest

from deploy.config import parse_config
from deploy.dev import MissingSecrets, dev_command, dev_workdir, resolve_env

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
    c = cfg('[app]\ntype = "static"\n[build]\nsteps = []\noutput = "out"\n')
    with pytest.raises(ValueError, match="static"):
        dev_command(c)
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_dev.py -v`
Expected: FAIL — `No module named 'deploy.dev'`

- [ ] **Step 3: Write `src/deploy/dev.py`**

```python
import os
import shlex
import subprocess
import threading
from pathlib import Path

from deploy.config import AppConfig
from deploy.proxy import serve
from deploy.runner import Runner
from deploy.secrets import missing_secrets, read_env_file

DEFAULT_DEV_PORT = 8000


class MissingSecrets(Exception):
    def __init__(self, names: list[str], descriptions: dict[str, str]):
        self.names = names
        detail = "\n".join(f"  {n} — {descriptions[n]}" for n in names)
        super().__init__(
            "missing secrets; add them to a gitignored .env in the repo root:\n"
            + detail
        )


def resolve_env(
    config: AppConfig, dotenv: dict[str, str], *, port: int
) -> dict[str, str]:
    """The environment `deploy dev` runs the app with. Fails up front when a
    declared secret has no value, rather than letting the app die confusingly."""
    absent = missing_secrets(config.secrets, dotenv)
    if absent:
        raise MissingSecrets(absent, config.secrets)

    env = dict(os.environ)
    env.update(config.env)
    env.update({name: dotenv[name] for name in config.secrets})
    # Safe to set PORT last and unconditionally: config.py reserves PORT and
    # PATH, so neither [env] nor [secrets] can contain them.
    env["PORT"] = str(port)
    return env


def dev_command(config: AppConfig) -> str:
    if config.is_static:
        raise ValueError("a static app has no start command")
    assert config.service is not None
    return config.dev_start or config.service.start


def dev_workdir(config: AppConfig, repo: Path) -> Path:
    sub = config.service.workdir if config.service else ""
    return repo / sub if sub else repo


def run_dev(
    config: AppConfig,
    repo: Path,
    *,
    port: int = DEFAULT_DEV_PORT,
    build: bool = False,
    prefix: bool = False,
    runner: Runner,
) -> int:
    """Run the app in the foreground. Returns the child's exit code."""
    env = resolve_env(config, read_env_file(repo / ".env"), port=port)

    if build and config.build:
        workdir = repo / config.build.workdir if config.build.workdir else repo
        for step in config.build.steps:
            print(f"[build] {step}")
            runner.run(shlex.split(step), cwd=workdir, env=env)

    if config.is_static:
        output = repo / config.build.output
        print(f"[dev] serving {output} on http://127.0.0.1:{port}")
        return subprocess.call(
            ["python", "-m", "http.server", str(port), "--directory", str(output)]
        )

    app_port = port
    if prefix:
        if config.nginx is None:
            raise ValueError("--prefix needs an [nginx] section with a path")
        app_port = _free_port()
        env["PORT"] = str(app_port)
        threading.Thread(
            target=serve,
            kwargs={
                "nginx": config.nginx,
                "upstream_port": app_port,
                "listen_port": port,
            },
            daemon=True,
        ).start()

    command = dev_command(config)
    print(f"[dev] {command}  (PORT={app_port})")
    return subprocess.call(
        ["/bin/bash", "-c", f"exec {command}"],
        cwd=dev_workdir(config, repo),
        env=env,
    )


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_dev.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 5: Commit**

```bash
git add src/deploy/dev.py tests/test_dev.py
git commit -m "feat: deploy dev with optional prefix proxy"
```

---

### Task 11: `install` and `update`

**Files:**
- Create: `src/deploy/health.py`
- Create: `src/deploy/commands.py`
- Test: `tests/test_health.py`
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: everything from Tasks 1–8.
- Produces: from health, `wait_healthy(port: int, path: str | None, *, timeout: float = 15.0) -> bool`. From commands, `load_app(name: str, *, paths: Paths, runner: Runner) -> tuple[AppConfig, Path]`, `install(name_or_url: str, *, paths: Paths, runner: Runner, prompt: Callable[[str, str], str]) -> int`, `update(name: str, *, paths: Paths, runner: Runner, prompt: Callable[[str, str], str]) -> int`. Both return a process exit code.

- [ ] **Step 1: Write `src/deploy/health.py` with its tests**

`src/deploy/health.py`:

```python
import socket
import time
import urllib.error
import urllib.request


def wait_healthy(port: int, path: str | None, *, timeout: float = 15.0) -> bool:
    """Poll until the service answers, or give up.

    With no health_path a successful TCP connect is the whole check; with one,
    the response must be 2xx.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _tcp_ok(port) and (path is None or _http_ok(port, path)):
            return True
        time.sleep(0.3)
    return False


def _tcp_ok(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _http_ok(port: int, path: str) -> bool:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError):
        return False
```

`tests/test_health.py`:

```python
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from deploy.health import wait_healthy


class Ok(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        code = 500 if self.path == "/bad" else 200
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def start():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Ok)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def test_a_listening_port_is_healthy_with_no_path():
    server, port = start()
    assert wait_healthy(port, None, timeout=2) is True
    server.shutdown()


def test_a_closed_port_is_unhealthy():
    server, port = start()
    server.shutdown()
    server.server_close()
    assert wait_healthy(port, None, timeout=1) is False


def test_a_2xx_health_path_is_healthy():
    server, port = start()
    assert wait_healthy(port, "/", timeout=2) is True
    server.shutdown()


def test_a_5xx_health_path_is_unhealthy():
    server, port = start()
    assert wait_healthy(port, "/bad", timeout=1) is False
    server.shutdown()
```

Run: `uv run pytest tests/test_health.py -v`
Expected: PASS, 4 tests.

- [ ] **Step 2: Write the failing command tests**

`tests/test_commands.py`:

```python
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
```

Note: `test_update_prompts_only_for_newly_declared_secrets` appends `NEW_SECRET = "another"` to the end of the TOML, which lands in the final `[secrets]` table. That is intentional — it is exactly how a new secret gets added to a real repo.

- [ ] **Step 3: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_commands.py -v`
Expected: FAIL — `No module named 'deploy.commands'`

- [ ] **Step 4: Write `src/deploy/commands.py`**

```python
import difflib
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from deploy.config import AppConfig, parse_config
from deploy.gitrepo import clone, head_commit, pull_ff_only, repo_url
from deploy.health import wait_healthy
from deploy.paths import SYSTEMD_UNIT, Paths
from deploy.ports import allocate_port, ports_in_use
from deploy.reconcile import apply_changes, owned_artifacts, plan_app_changes, plan_changes
from deploy.render import render
from deploy.runner import Runner
from deploy.secrets import merge_secrets, missing_secrets, read_env_file
from deploy.static import publish

SYSTEMCTL = "/usr/bin/systemctl"
Prompt = Callable[[str, str], str]


def load_app(name: str, *, paths: Paths, runner: Runner) -> tuple[AppConfig, Path]:
    repo = paths.clone_dir(name)
    config_file = repo / "deploy.toml"
    if not config_file.exists():
        raise FileNotFoundError(f"{config_file} does not exist")
    return parse_config(config_file.read_text(), repo_name=name), repo


def installed_apps(paths: Paths) -> list[str]:
    names = {p.stem for p in paths.units.glob("*.service")} if paths.units.is_dir() else set()
    if paths.nginx.is_dir():
        names |= {p.stem for p in paths.nginx.glob("*.conf")}
    return sorted(names)


def _check_route_free(config: AppConfig, *, paths: Paths) -> None:
    """Two apps claiming one route makes nginx reject the whole config, so
    catch it here where the message can name both."""
    if config.nginx is None or not paths.nginx.is_dir():
        return
    for existing in paths.nginx.glob("*.conf"):
        if existing.stem == config.name:
            continue
        if f"location {config.nginx.path} {{" in existing.read_text():
            raise ValueError(
                f"route {config.nginx.path} is already served by {existing.stem}"
            )


def _run_build(config: AppConfig, repo: Path, *, runner: Runner) -> None:
    """Build before anything is written, so a failure leaves the running
    service untouched."""
    if not config.build or not config.build.steps:
        return
    workdir = repo / config.build.workdir if config.build.workdir else repo
    for step in config.build.steps:
        print(f"[build] {step}")
        try:
            runner.run(shlex.split(step), cwd=workdir)
        except subprocess.CalledProcessError:
            print(
                f"{config.name}: build step failed: {step}\n"
                f"  nothing was deployed and the running service is unchanged.\n"
                f"  the working tree in {repo} is now ahead of what is running.",
                file=sys.stderr,
            )
            raise


def _collect_secrets(config: AppConfig, *, paths: Paths, prompt: Prompt) -> None:
    if not config.secrets:
        return
    env_file = paths.env_file(config.name)
    absent = missing_secrets(config.secrets, read_env_file(env_file))
    if not absent:
        return
    merge_secrets(env_file, {n: prompt(n, config.secrets[n]) for n in absent})


def _deploy(
    config: AppConfig,
    repo: Path,
    *,
    paths: Paths,
    runner: Runner,
    prompt: Prompt,
    first_install: bool,
    code_changed: bool = False,
) -> int:
    _check_route_free(config, paths=paths)

    port = None
    if not config.is_static:
        assert config.service is not None
        port = allocate_port(paths, name=config.name, pinned=config.service.port)

    _collect_secrets(config, paths=paths, prompt=prompt)
    _run_build(config, repo, runner=runner)

    if config.is_static:
        assert config.build is not None
        publish(
            repo / config.build.output,
            name=config.name,
            commit=head_commit(repo, runner=runner)[:12],
            paths=paths,
        )

    # plan_app_changes, not plan_changes: it is the only thing that derives
    # what this app previously owned but no longer wants, so a service that
    # becomes static (or drops its [nginx] section) has its stale file removed
    # instead of left on disk holding a port forever.
    changes = plan_app_changes(config.name, render(config, port, paths), paths)
    apply_changes(changes, runner=runner)

    if config.is_static:
        print(f"{config.name}: published")
        return 0

    unit_changed = any(c.kind is SYSTEMD_UNIT for c in changes)
    unit = paths.systemd_unit_file(config.name)

    if first_install:
        runner.run(["sudo", SYSTEMCTL, "link", str(unit)])
        runner.run(["sudo", SYSTEMCTL, "enable", config.name])
        runner.run(["sudo", SYSTEMCTL, "start", config.name])
    elif unit_changed or code_changed:
        # New commits matter even when the unit is byte-identical: the running
        # process is still executing the old code until it is restarted.
        runner.run(["sudo", SYSTEMCTL, "restart", config.name])

    assert port is not None
    health_path = config.service.health_path if config.service else None
    if not wait_healthy(port, health_path):
        print(f"{config.name}: FAILED health check on port {port}")
        runner.run(
            ["journalctl", "-u", config.name, "-n", "20", "--no-pager"], check=False
        )
        return 1

    print(f"{config.name}: healthy on port {port}")
    return 0


def install(
    name_or_url: str, *, paths: Paths, runner: Runner, prompt: Prompt
) -> int:
    url = repo_url(name_or_url)
    repo_name = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    repo = paths.clone_dir(repo_name)

    if not repo.exists():
        clone(url, repo, runner=runner)

    config, repo = load_app(repo_name, paths=paths, runner=runner)

    # The repo name and the app name can differ (boggle-solver -> boggle). The
    # clone directory must match the app name, because the unit's
    # WorkingDirectory is derived from it.
    if config.name != repo_name:
        wanted = paths.clone_dir(config.name)
        if wanted.exists():
            raise ValueError(f"{wanted} already exists; cannot rename {repo}")
        repo.rename(wanted)
        repo = wanted

    first = not paths.systemd_unit_file(config.name).exists()
    return _deploy(
        config, repo, paths=paths, runner=runner, prompt=prompt, first_install=first
    )


def update(name: str, *, paths: Paths, runner: Runner, prompt: Prompt) -> int:
    config, repo = load_app(name, paths=paths, runner=runner)
    moved = pull_ff_only(repo, runner=runner)
    # Re-read: the pull may have changed deploy.toml itself.
    config, repo = load_app(name, paths=paths, runner=runner)

    port = None
    if not config.is_static:
        assert config.service is not None
        port = allocate_port(paths, name=config.name, pinned=config.service.port)

    # A newly declared secret is work to do even when nothing else changed,
    # so it has to be part of the up-to-date test rather than checked after it.
    needs_secret = bool(
        missing_secrets(config.secrets, read_env_file(paths.env_file(config.name)))
    )
    if (
        not moved
        and not needs_secret
        and not plan_app_changes(config.name, render(config, port, paths), paths)
    ):
        print(f"{name}: already up to date")
        return 0

    return _deploy(
        config,
        repo,
        paths=paths,
        runner=runner,
        prompt=prompt,
        first_install=False,
        code_changed=moved,
    )
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `uv run pytest tests/test_commands.py -v`
Expected: PASS, 12 tests.

If `test_update_with_no_changes_reports_up_to_date_and_does_not_restart` fails because `pull_ff_only` cannot reach an origin, give the test repo an origin pointing at itself: `git remote add origin <path>` plus `git branch --set-upstream-to`. Alternatively make `make_repo` clone from a bare origin the way `tests/test_gitrepo.py` does.

- [ ] **Step 6: Commit**

```bash
git add src/deploy/health.py src/deploy/commands.py tests/test_health.py tests/test_commands.py
git commit -m "feat: install and update commands"
```

---

### Task 12: The remaining commands and the CLI

**Files:**
- Modify: `src/deploy/commands.py`
- Create: `src/deploy/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `AppStatus(name, port, route, active, commit)` and `status_of(name, *, paths, runner) -> AppStatus`; `list_apps(*, paths, runner) -> list[AppStatus]`; `diff(name: str | None, *, paths, runner) -> int`; `remove(name: str, *, paths, runner, purge: bool, confirm: Callable[[str], bool]) -> int`; `main(argv: list[str] | None = None) -> int` in `cli.py`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
import pytest

from deploy.cli import build_parser
from deploy.commands import diff, install, list_apps, remove
from deploy.paths import Paths
from deploy.runner import RecordingRunner
from tests.test_commands import POKEMON_TOML, answer, make_repo


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
    assert not runner.ran("fetch")


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
```

- [ ] **Step 2: Run the tests and confirm they fail**

Run: `uv run pytest tests/test_cli.py -v`
Expected: FAIL — `cannot import name 'build_parser'`

- [ ] **Step 3: Append the remaining commands to `src/deploy/commands.py`**

All imports this block needs are already at the top of the file from Step 4.

```python
@dataclass(frozen=True)
class AppStatus:
    name: str
    port: int | None
    route: str | None
    active: str
    commit: str
    behind: int | None = None


def status_of(
    name: str, *, paths: Paths, runner: Runner, fetch: bool = False
) -> AppStatus:
    port = ports_in_use(paths).get(name)
    route = None
    config_file = paths.clone_dir(name) / "deploy.toml"
    if config_file.exists():
        config = parse_config(config_file.read_text(), repo_name=name)
        route = config.nginx.path if config.nginx else None

    active = "unknown"
    if port is not None:
        result = runner.run(
            ["sudo", SYSTEMCTL, "is-active", name], check=False
        )
        active = (result.stdout or "").strip() or "unknown"

    commit = ""
    behind = None
    repo = paths.clone_dir(name)
    if (repo / ".git").exists():
        commit = head_commit(repo, runner=runner)[:8]
        if fetch:
            behind = _commits_behind(repo, runner=runner)

    return AppStatus(
        name=name,
        port=port,
        route=route,
        active=active,
        commit=commit,
        behind=behind,
    )


def _commits_behind(repo: Path, *, runner: Runner) -> int | None:
    """How many commits origin is ahead by. None when there is no upstream."""
    runner.run(["git", "-C", str(repo), "fetch", "--quiet"], check=False)
    result = runner.run(
        ["git", "-C", str(repo), "rev-list", "--count", "HEAD..@{u}"], check=False
    )
    if result.returncode != 0:
        return None
    try:
        return int((result.stdout or "").strip())
    except ValueError:
        return None


def list_apps(*, paths: Paths, runner: Runner, fetch: bool = False) -> list[AppStatus]:
    return [
        status_of(n, paths=paths, runner=runner, fetch=fetch)
        for n in installed_apps(paths)
    ]


def diff(name: str | None, *, paths: Paths, runner: Runner) -> int:
    """Show what would change. Writes nothing and runs nothing."""
    names = [name] if name else installed_apps(paths)
    any_changes = False
    for app in names:
        config, _ = load_app(app, paths=paths, runner=runner)
        port = None
        if not config.is_static:
            assert config.service is not None
            port = allocate_port(paths, name=app, pinned=config.service.port)
        for change in plan_app_changes(app, render(config, port, paths), paths):
            any_changes = True
            before = (change.before or "").splitlines(keepends=True)
            after = (change.after or "").splitlines(keepends=True)
            print(
                "".join(
                    difflib.unified_diff(
                        before, after, f"a{change.path}", f"b{change.path}"
                    )
                )
            )
    if not any_changes:
        print("no changes")
    return 0


def restart(name: str, *, paths: Paths, runner: Runner) -> int:
    runner.run(["sudo", SYSTEMCTL, "restart", name])
    config, _ = load_app(name, paths=paths, runner=runner)
    port = ports_in_use(paths).get(name)
    if port is None:
        return 0
    health_path = config.service.health_path if config.service else None
    return 0 if wait_healthy(port, health_path) else 1


def logs(name: str, *, follow: bool, runner: Runner) -> int:
    argv = ["journalctl", "-u", name, "-n", "50"]
    if follow:
        argv.append("-f")
    else:
        argv.append("--no-pager")
    return runner.run(argv, check=False).returncode


def remove(
    name: str,
    *,
    paths: Paths,
    runner: Runner,
    purge: bool,
    confirm: Callable[[str], bool],
) -> int:
    unit = paths.systemd_unit_file(name)
    if unit.exists():
        runner.run(["sudo", SYSTEMCTL, "stop", name], check=False)
        runner.run(["sudo", SYSTEMCTL, "disable", name], check=False)

    # owned_artifacts derives the set from ARTIFACT_KINDS, so a kind added
    # later is removed here too without editing this function.
    changes = plan_changes((), remove=owned_artifacts(name, paths))
    apply_changes(changes, runner=runner)

    if purge:
        targets = [paths.clone_dir(name), paths.env_file(name)]
        existing = [t for t in targets if t.exists()]
        listing = "\n".join(f"  {t}" for t in existing)
        if not existing or not confirm(f"permanently delete:\n{listing}\n"):
            print("keeping the clone and env file")
            return 0
        for target in existing:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
    print(f"{name}: removed")
    return 0
```

- [ ] **Step 4: Write `src/deploy/cli.py`**

```python
import argparse
import getpass
import sys
from pathlib import Path

from deploy import commands
from deploy.config import parse_config
from deploy.dev import DEFAULT_DEV_PORT, run_dev
from deploy.paths import Paths
from deploy.runner import RealRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deploy")
    parser.add_argument(
        "--root",
        type=Path,
        help="redirect the whole on-disk layout under this directory (testing)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("install", help="clone, build, wire up and start an app")
    p.add_argument("name")

    p = sub.add_parser("update", help="pull, build, apply changes and restart")
    p.add_argument("name", nargs="?")
    p.add_argument("--all", action="store_true")

    p = sub.add_parser("list", help="show installed apps")
    p.add_argument("--fetch", action="store_true", help="also report commits behind")

    p = sub.add_parser("diff", help="show what would change; writes nothing")
    p.add_argument("name", nargs="?")

    p = sub.add_parser("restart")
    p.add_argument("name")

    p = sub.add_parser("logs")
    p.add_argument("name")
    p.add_argument("-f", "--follow", action="store_true")

    p = sub.add_parser("remove")
    p.add_argument("name")
    p.add_argument("--purge", action="store_true", help="also delete clone and secrets")

    p = sub.add_parser("dev", help="run this repo locally")
    p.add_argument("--prefix", action="store_true", help="serve under the nginx route")
    p.add_argument("--build", action="store_true", help="run build steps first")
    p.add_argument("--port", type=int, default=DEFAULT_DEV_PORT)

    return parser


def _prompt(name: str, description: str) -> str:
    return getpass.getpass(f"{name} ({description}): ")


def _confirm(message: str) -> bool:
    return input(f"{message}type 'yes' to confirm: ").strip() == "yes"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    paths = Paths.under(args.root) if args.root else Paths.default()
    runner = RealRunner()

    if args.command == "install":
        return commands.install(
            args.name, paths=paths, runner=runner, prompt=_prompt
        )

    if args.command == "update":
        names = (
            commands.installed_apps(paths) if args.all else [args.name]
        )
        if not names or names == [None]:
            print("give an app name or --all", file=sys.stderr)
            return 2
        return max(
            commands.update(n, paths=paths, runner=runner, prompt=_prompt)
            for n in names
        )

    if args.command == "list":
        for app in commands.list_apps(paths=paths, runner=runner, fetch=args.fetch):
            port = app.port if app.port is not None else "-"
            behind = "" if app.behind in (None, 0) else f"  ({app.behind} behind)"
            print(
                f"{app.name:<12} {str(port):<6} {app.route or '-':<14} "
                f"{app.active:<10} {app.commit}{behind}"
            )
        return 0

    if args.command == "diff":
        return commands.diff(args.name, paths=paths, runner=runner)

    if args.command == "restart":
        return commands.restart(args.name, paths=paths, runner=runner)

    if args.command == "logs":
        return commands.logs(args.name, follow=args.follow, runner=runner)

    if args.command == "remove":
        return commands.remove(
            args.name,
            paths=paths,
            runner=runner,
            purge=args.purge,
            confirm=_confirm,
        )

    if args.command == "dev":
        repo = Path.cwd()
        config = parse_config(
            (repo / "deploy.toml").read_text(), repo_name=repo.name
        )
        return run_dev(
            config,
            repo,
            port=args.port,
            build=args.build,
            prefix=args.prefix,
            runner=runner,
        )

    return 2
```

- [ ] **Step 5: Give `main()` top-level error handling**

Every failure below is something a user can cause with a bad `deploy.toml` or
a broken server state, and none should reach the terminal as a traceback.
`ApplyFailed` matters most: it carries what *did* take effect before the
failure, which is exactly what someone needs before deciding whether to re-run.

Rename the existing `main` to `_run`, leaving its body unchanged, and add:

```python
from deploy.config import ConfigError
from deploy.gitrepo import DirtyRepo
from deploy.reconcile import ApplyFailed, ForeignFile


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except ApplyFailed as exc:
        print(f"error: {exc}", file=sys.stderr)
        if exc.actions_completed:
            # The failure happened partway. Say what already took effect, so
            # the user knows whether the box is in the old state or a mixed one.
            done = ", ".join(sorted(exc.actions_completed))
            print(f"  these already took effect: {done}", file=sys.stderr)
        if exc.unrestored:
            stuck = ", ".join(str(p) for p in exc.unrestored)
            print(f"  could not roll back: {stuck} — check by hand", file=sys.stderr)
        return 1
    except (ConfigError, ForeignFile, DirtyRepo, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
```

`ValueError` is listed because `repo_url`, `allocate_port` and
`static.publish` all raise it for bad input with messages already written for
a human. `subprocess.CalledProcessError` is deliberately NOT caught — a failed
build step already printed its own output, and the traceback is diagnostic
rather than noise.

- [ ] **Step 6: Test the error handling**

Append to `tests/test_cli.py`:

```python
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
```

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -v`
Expected: PASS. Every test from Tasks 1–12.

- [ ] **Step 8: Verify the CLI runs end to end against a fake root**

```bash
mkdir -p /tmp/deploy-check/apps/demo
cd /tmp/deploy-check/apps/demo
git init -q && cat > deploy.toml <<'EOF'
[service]
start = "python3 -m http.server $PORT --bind 127.0.0.1"
[nginx]
path = "/demo/"
EOF
git add -A && git -c user.email=t@t -c user.name=t commit -qm init
cd - >/dev/null
uv run deploy --root /tmp/deploy-check diff demo
```

Expected: a unified diff showing the unit and nginx snippet that *would* be written, and nothing created under `/tmp/deploy-check/etc`.

- [ ] **Step 9: Commit**

```bash
git add src/deploy/commands.py src/deploy/cli.py tests/test_cli.py
git commit -m "feat: list, diff, restart, logs, remove, and the CLI"
```

---

### Task 13: Server bootstrap and the pokemon migration

This task runs against the live server. Everything before it was testable on a laptop; this is the first time anything real moves. Do it in the order given — each step is reversible until step 7.

**Files:**
- Create: `README.md`
- Create: `bootstrap.sh`
- Create (in the pokemon repo, not this one): `npd.toml`

**Interfaces:**
- Consumes: the whole tool.
- Produces: a working deployment of pokemon managed by `npd`.

- [ ] **Step 1: Write `bootstrap.sh`**

```bash
#!/bin/bash
# One-time server setup for npd. Run as nathan on natpat.net.
# Everything here needs a sudo password once; the tool never does afterwards.
set -euo pipefail

echo "==> creating directories owned by $USER"
sudo mkdir -p /etc/npd/env /etc/npd/systemd /etc/nginx/npd.d /var/www/npd
sudo chown -R "$USER:$USER" /etc/npd /etc/nginx/npd.d /var/www/npd
sudo chmod 700 /etc/npd/env

echo "==> granting passwordless nginx test and reload"
# systemctl is already NOPASSWD via /etc/sudoers.d/site.
sudo tee /etc/sudoers.d/npd >/dev/null <<EOF
$USER ALL=(ALL) NOPASSWD: /usr/sbin/nginx -t
$USER ALL=(ALL) NOPASSWD: /usr/sbin/nginx -s reload
EOF
sudo chmod 440 /etc/sudoers.d/npd
sudo visudo -c -f /etc/sudoers.d/npd

echo "==> checking the nginx include"
if ! grep -q 'include /etc/nginx/npd.d/' /etc/nginx/sites-available/natpat.net; then
  echo "MANUAL STEP REQUIRED: add this line inside the natpat.net server block:"
  echo "        include /etc/nginx/npd.d/*.conf;"
  exit 1
fi

sudo nginx -t
echo "==> bootstrap complete"
```

- [ ] **Step 2: Add the nginx include by hand and verify**

On the server, edit `/etc/nginx/sites-available/natpat.net` and add `include /etc/nginx/npd.d/*.conf;` inside the `server { ... }` block that has `server_name natpat.net www.natpat.net;` and `listen 443`. Then:

Run: `sudo nginx -t && sudo systemctl reload nginx`
Expected: `syntax is ok` / `test is successful`, and natpat.net still serves. An empty `npd.d` makes the include a no-op.

- [ ] **Step 3: Run the bootstrap and install the tool**

```bash
bash bootstrap.sh
uv tool install git+ssh://git@github.com/gnatpat/deploy
npd list
```

Expected: bootstrap reports complete; `npd list` prints nothing and exits 0.

- [ ] **Step 4: Add `npd.toml` to the pokemon repo**

In the pokemon repo (locally, then push). The values come from the spec's Inventory — the port is pinned and `strip_prefix` is `false` because the live `proxy_pass` has no trailing slash:

```toml
[app]
name = "pokemon"

[build]
workdir = "pokemon-cards-app"
steps = ["npm install", "npm run build"]

[service]
workdir = "server"
start = "uv run uvicorn main:app --host 127.0.0.1 --port $PORT"
port = 8151
# Measured on the server, 2026-09-12: pokemon answers 404 on "/" and 200 on
# "/pokemon/". It is the one app with strip_prefix = false, so it receives its
# own prefix and its routes live under it. health_path = "/" — as this plan
# originally said — would have reported FAILED mid-cutover on a deploy that
# actually worked.
health_path = "/pokemon/"

[nginx]
path = "/pokemon/"
strip_prefix = false
client_max_body_size = "10m"

[secrets]
COLLECTION_PASSWORD = "password for the collection upload endpoint"
```

- [ ] **Step 4a: Find out what the app answers on, before trusting a health check**

The health check GETs `http://127.0.0.1:<port><health_path>`. Getting it wrong
means the tool reports `FAILED health check` in the middle of a cutover that
actually worked, and you cannot tell a false alarm from a real failure at the
moment you least want the ambiguity. On the server, with the old service still
running:

```bash
for path in / /pokemon/ /pokemon/api/collection; do
  printf '%-26s %s\n' "$path" "$(curl -s -o /dev/null -w '%{http_code}' 127.0.0.1:8151$path)"
done
```

If `/` returns 2xx you may set `health_path = "/"`. If it 404s — likely, since
this app receives its own prefix — leave `health_path` out, or set it to a path
that did answer. Repeat for each app you migrate: `/blog` and `/crochet` strip
their prefix so `/` is the right probe for them, but confirm rather than assume.

- [ ] **Step 4b: Commit pokemon's dirty package-lock.json**

Measured on the server, 2026-09-12: `~/pokemon` has one modified file,
`pokemon-cards-app/package-lock.json` — almost certainly rewritten by an
`npm install` during an earlier manual deploy. `npd update` refuses a dirty
tree, so this blocks every future update of this app until it is dealt with.

```bash
cd ~/pokemon && git status --porcelain     # expect: M pokemon-cards-app/package-lock.json
git diff pokemon-cards-app/package-lock.json | head
```

Commit it if the change is legitimate (it usually is — a lockfile update
belongs in the repo), or `git checkout` it if it is churn. `~/crochet` and
`~/blog` were both clean, so this is pokemon-only.

- [ ] **Step 4c: Check the clone is clean, or every future update will fail**

`npd update` refuses to pull over a dirty tree, and it counts UNTRACKED
files as dirty — deliberately, because live SQLite databases sit inside these
clones. Run `cd ~/pokemon && git status --porcelain`. If it lists anything (a
`.db` file, `node_modules`, build output), add it to `.gitignore` and commit
that first, or every future `npd update` for this app fails with "has
uncommitted changes".

- [ ] **Step 5: Verify the prefix behaviour locally before touching the server**

```bash
cd ~/Documents/pokemon-cards
echo "COLLECTION_PASSWORD=anything" > .env
grep -q '^\.env$' .gitignore || echo '.env' >> .gitignore
npd dev --prefix --build
```

Open `http://127.0.0.1:8000/pokemon/` and click through the app. Expected: it behaves as it does in production. If routes 404, `strip_prefix` is wrong — fix it here, not after deploying.

- [ ] **Step 6: Preview the server change without applying it**

On the server:

```bash
mv ~/pokemon ~/apps/pokemon     # move, do NOT re-clone: collection.db lives inside
cd ~/apps/pokemon && git pull
npd diff pokemon
```

Expected: a diff creating `/etc/npd/systemd/pokemon.service` and `/etc/nginx/npd.d/pokemon.conf`. Read the `proxy_pass` line and confirm it matches the live one in `sites-available/natpat.net` exactly, trailing slash included.

- [ ] **Step 7: Cut over**

```bash
sudo systemctl stop pokemon
sudo systemctl disable pokemon
sudo rm /etc/systemd/system/pokemon.service
# remove the two `location /pokemon` blocks from sites-available/natpat.net
sudo nginx -t
npd install pokemon
```

Expected: `npd install` prompts for `COLLECTION_PASSWORD`, builds, writes both files, links and starts the unit, and reports `pokemon: healthy on port 8151`.

- [ ] **Step 8: Verify in a browser and clean up**

Visit `https://natpat.net/pokemon/`, load a collection, and confirm the upload endpoint still accepts the password. Then:

```bash
rm ~/run-pokemon.sh ~/update-pokemon.sh
npd list
```

Expected: pokemon listed as active on 8151. **Rotate `COLLECTION_PASSWORD`** — the old value was in a plaintext shell script for months.

- [ ] **Step 9: Confirm idempotency on the real box**

```bash
npd update pokemon
npd diff pokemon
```

Expected: `already up to date`, then `no changes`. No restart, no reload.

- [ ] **Step 10: Write `README.md` and commit**

`README.md` covers: what the tool does, the `npd.toml` reference with every key, the commands, the one-time bootstrap, and the migration runbook for the remaining apps (blog, crochet, boggle), pointing at the spec's Inventory for each one's values.

```bash
git add README.md bootstrap.sh
git commit -m "docs: README and server bootstrap script"
```

---

## Remaining migrations

Not tasks — do these once pokemon has been stable for a few days. Each follows Task 13 steps 4–8 with values from the spec's Inventory.

- **crochet** — `start = "uv run uvicorn server:app --host 127.0.0.1 --port $PORT"`, `workdir = "server"`, `port = 8152`, `path = "/crochet/"`, `strip_prefix = true`, no secrets.
- **blog** — `port = 8080`, `path = "/blog"`, `strip_prefix = true`, `env.INSTANCE_PATH = "/blog"`. Its start command **must** gain an explicit port: `waitress-serve --port=$PORT --call 'blog:create_app'` — except that contains single quotes, which `parse_config` rejects. Use `waitress-serve --port=$PORT --call blog:create_app` (waitress does not need the quoting) and confirm it starts before cutting over.
- **boggle** — `type = "static"`, `output = "static"`, `path = "/boggle/"`. After cutover, delete `/resources/boggle/` and `/public_html/www/boggle/` and drop `release.sh` from the repo.
- **shogi** and **wedding** — decommission per the spec's Decommissioning section rather than migrating.
