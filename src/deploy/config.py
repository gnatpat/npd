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
