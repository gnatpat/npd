import re
import tomllib
from dataclasses import dataclass, field
from typing import Any

VALID_TYPES = ("service", "static")
NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
NGINX_PATH_PATTERN = re.compile(r"^[A-Za-z0-9/_.~-]+$")
BODY_SIZE_PATTERN = re.compile(r"^\d+[kKmMgG]?$")
RESERVED_ENV_KEYS = ("PORT", "PATH")
MIN_UNPRIVILEGED_PORT = 1024
MAX_PORT = 65535


def _has_invalid_chars(value: str) -> str | None:
    """Check for control chars, double quote, or backslash. Return char description or None."""
    for i, char in enumerate(value):
        if char < "\x20":  # Control character
            if char == "\n":
                return "newline"
            elif char == "\r":
                return "carriage return"
            else:
                return f"control character (U+{ord(char):04X})"
        elif char == '"':
            return "double quote"
        elif char == "\\":
            return "backslash"
    return None


class ConfigError(Exception):
    """A deploy.toml that cannot be used. The message is shown to the user."""


def _table(raw: dict[str, Any], key: str) -> dict[str, Any]:
    """Extract a table from the config, raising ConfigError if it's not a dict."""
    value = raw.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{key}] must be a table, got {type(value).__name__}")
    return value


def _string(value: Any, key: str, where: str) -> str:
    """Validate that a value is a string, raising ConfigError if not."""
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key} must be a string, got {type(value).__name__}")
    return value


def _validate_app_name(name: str) -> None:
    """Validate the app name is a safe single path segment."""
    if not name:
        raise ConfigError("app name cannot be empty")
    if "/" in name or "\\" in name:
        raise ConfigError(
            f"app name {name!r} contains slashes; "
            "it must be a single path segment (letters, digits, hyphen, underscore, dot)"
        )
    if name == "." or name == "..":
        raise ConfigError(
            f"app name {name!r} is not allowed; "
            "it must be a single path segment (letters, digits, hyphen, underscore, dot)"
        )
    if not NAME_PATTERN.match(name):
        raise ConfigError(
            f"app name {name!r} contains invalid characters; "
            "it must contain only letters, digits, hyphen, underscore, or dot"
        )


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

    app = _table(raw, "app")
    app_type = app.get("type", "service")
    if app_type not in VALID_TYPES:
        raise ConfigError(
            f"app.type must be one of {', '.join(VALID_TYPES)}, got {app_type!r}"
        )

    name_field = app.get("name")
    if name_field is not None:
        _string(name_field, "name", "app")
    name = name_field or repo_name
    _validate_app_name(name)
    env_table = _table(raw, "env")
    env = {str(k): str(v) for k, v in env_table.items()}
    secrets_table = _table(raw, "secrets")
    secrets = {str(k): str(v) for k, v in secrets_table.items()}

    for key in env:
        if key in RESERVED_ENV_KEYS:
            raise ConfigError(
                f"{key!r} may not be set in [env]; PORT is assigned by deploy "
                "and PATH is set explicitly in the generated unit"
            )
    for key in secrets:
        if key in RESERVED_ENV_KEYS:
            raise ConfigError(
                f"{key!r} may not be set in [secrets]; PORT is assigned by deploy "
                "and PATH is set explicitly in the generated unit"
            )

    # Validate env and secrets for invalid characters
    for key, value in env.items():
        invalid = _has_invalid_chars(key)
        if invalid:
            raise ConfigError(f"env key {key!r} contains {invalid}")
        invalid = _has_invalid_chars(value)
        if invalid:
            raise ConfigError(f"env value for {key!r} contains {invalid}")
    for key, value in secrets.items():
        invalid = _has_invalid_chars(key)
        if invalid:
            raise ConfigError(f"secrets key {key!r} contains {invalid}")
        invalid = _has_invalid_chars(value)
        if invalid:
            raise ConfigError(f"secrets value for {key!r} contains {invalid}")

    both = sorted(set(env) & set(secrets))
    if both:
        raise ConfigError(
            f"{', '.join(both)} appears in both [env] and [secrets]; pick one"
        )

    build = _parse_build(_table(raw, "build"))
    nginx = _parse_nginx(_table(raw, "nginx"), app_type)

    if app_type == "static":
        if "service" in raw:
            raise ConfigError("a static app must not declare [service]")
        if secrets:
            raise ConfigError("a static app must not declare [secrets]")
        if build is None or not build.output:
            raise ConfigError("a static app requires build.output")
        if nginx is None:
            raise ConfigError("a static app requires an [nginx] section with a path")
        service = None
    else:
        service = _parse_service(_table(raw, "service"))

    dev = _table(raw, "dev")
    dev_start = dev.get("start")
    if dev_start is not None:
        dev_start = _string(dev_start, "start", "dev")
        if "'" in dev_start:
            raise ConfigError(
                "dev.start may not contain a single quote; it is embedded the same "
                "way service.start is when it runs"
            )
        invalid = _has_invalid_chars(dev_start)
        if invalid:
            raise ConfigError(f"dev.start contains {invalid}")

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
    if not raw:
        return None
    steps_raw = raw.get("steps", [])
    if not isinstance(steps_raw, list):
        raise ConfigError(f"build.steps must be a list, got {type(steps_raw).__name__}")
    steps = tuple(str(s) for s in steps_raw)
    return BuildConfig(
        steps=steps,
        workdir=str(raw.get("workdir", "")),
        output=str(raw.get("output", "")),
    )


def _parse_service(raw: dict[str, Any] | None) -> ServiceConfig:
    if not raw:
        raise ConfigError("a service app requires a [service] section")
    start = raw.get("start")
    if not start:
        raise ConfigError("service.start is required")
    start = _string(start, "start", "service")
    if "'" in start:
        raise ConfigError(
            "service.start may not contain a single quote; it is embedded in the "
            "unit's ExecStart as /bin/bash -c '...'"
        )
    invalid = _has_invalid_chars(start)
    if invalid:
        raise ConfigError(f"service.start contains {invalid}")
    port = raw.get("port")
    if port is not None:
        if isinstance(port, bool) or not isinstance(port, int):
            raise ConfigError(f"service.port must be an integer, got {type(port).__name__}")
        if port < MIN_UNPRIVILEGED_PORT:
            raise ConfigError(
                f"service.port {port} is below {MIN_UNPRIVILEGED_PORT}; privileged "
                "ports are not allowed since services run as a normal user"
            )
        if port > MAX_PORT:
            raise ConfigError(
                f"service.port {port} is above the maximum valid port {MAX_PORT}"
            )
    return ServiceConfig(
        start=start,
        workdir=str(raw.get("workdir", "")),
        health_path=raw.get("health_path"),
        port=port,
    )


def _parse_nginx(raw: dict[str, Any] | None, app_type: str) -> NginxConfig | None:
    if not raw:
        return None
    path = str(raw.get("path", ""))
    if not path.startswith("/"):
        raise ConfigError(f"nginx.path must start with '/', got {path!r}")
    if path == "/":
        raise ConfigError(
            'nginx.path may not be "/" — the site root is served by the static site, '
            "not by a managed app"
        )
    if not NGINX_PATH_PATTERN.match(path):
        raise ConfigError(
            f"nginx.path {path!r} contains invalid characters; "
            "it may only contain letters, digits, and / - _ . ~"
        )
    if app_type == "static" and "strip_prefix" in raw:
        raise ConfigError("strip_prefix is meaningless for a static app")
    if app_type == "static" and not path.endswith("/"):
        raise ConfigError(
            f'a static app\'s nginx.path must end with "/" (got {path!r})'
        )
    size = raw.get("client_max_body_size")
    size_str = str(size) if size is not None else None
    if size_str is not None and not BODY_SIZE_PATTERN.match(size_str):
        raise ConfigError(
            f"nginx.client_max_body_size {size_str!r} is invalid; it must be digits "
            "optionally followed by a single k, m, or g (e.g. '10m')"
        )
    return NginxConfig(
        path=path,
        strip_prefix=bool(raw.get("strip_prefix", True)),
        client_max_body_size=size_str,
    )
