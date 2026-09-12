from pathlib import Path

from deploy.config import AppConfig
from deploy.paths import MANAGED_HEADER, Paths


def _escape_unit_percent(value: str) -> str:
    """Escape systemd's '%' specifier syntax so a literal '%' in a value (an
    env var, a start command) round-trips instead of being expanded (e.g.
    '%H' -> hostname) or, for a trailing bare '%', failing unit parsing."""
    return value.replace("%", "%%")


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
        f'Environment="PATH={UNIT_PATH}"',
        f"Environment=\"PORT={port}\"",
    ]
    for key in sorted(config.env):
        value = _escape_unit_percent(config.env[key])
        lines.append(f'Environment="{key}={value}"')
    if config.secrets:
        lines.append(f"EnvironmentFile={paths.env_file(config.name)}")
    start = _escape_unit_percent(config.service.start)
    lines += [
        f"ExecStart=/bin/bash -c 'exec {start}'",
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
        files[paths.unit_file(config.name)] = render_unit(config, port, paths)
    if config.nginx is not None:
        files[paths.nginx_file(config.name)] = render_nginx(config, port, paths)
    return files
