from dataclasses import dataclass
from pathlib import Path

from npd import settings
from npd.config import AppConfig, NginxConfig
from npd.paths import ArtifactKind, MANAGED_HEADER, NGINX_SNIPPET, Paths, SYSTEMD_UNIT


@dataclass(frozen=True)
class Artifact:
    """One generated file: what kind it is, where it goes, and its exact
    contents. The kind travels with the file instead of being re-derived
    downstream from its path."""

    kind: ArtifactKind
    path: Path
    contents: str


def _escape_unit_percent(value: str) -> str:
    """Escape systemd's '%' specifier syntax so a literal '%' in a value (an
    env var, a start command) round-trips instead of being expanded (e.g.
    '%H' -> hostname) or, for a trailing bare '%', failing unit parsing."""
    return value.replace("%", "%%")


def _static_location_lines(config: AppConfig, paths: Paths) -> list[str]:
    return [
        f"    alias {paths.static / config.name}/;",
        "    try_files $uri $uri/ =404;",
    ]


def _proxy_location_lines(nginx: NginxConfig, port: int | None, path: str) -> list[str]:
    lines = []
    if nginx.client_max_body_size:
        lines.append(f"    client_max_body_size {nginx.client_max_body_size};")
    lines.append("    proxy_set_header Host $host;")
    lines.append("    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;")
    lines.append("    proxy_set_header X-Forwarded-Proto $scheme;")
    lines.append(f"    proxy_set_header X-Forwarded-Prefix {path};")
    # The trailing slash is the whole of strip_prefix: with it nginx
    # replaces the matched prefix, without it the full URI is passed on.
    suffix = "/" if nginx.strip_prefix else ""
    lines.append(f"    proxy_pass http://127.0.0.1:{port}{suffix};")
    return lines


def render_nginx_snippet(config: AppConfig, port: int | None, paths: Paths) -> str:
    """The location block(s) for one app. Pure: no IO, no subprocess."""
    if config.nginx is None:
        raise ValueError(f"{config.name} has no [nginx] section to render")

    path = config.nginx.path
    lines = [MANAGED_HEADER]

    # nginx treats /x and /x/ as different locations; a trailing-slash route
    # needs an explicit redirect or the bare URL 404s. The site root is the
    # exception: there is no bare form of "/" to redirect from, and
    # `location =  { ... }` would not parse.
    bare = path.rstrip("/")
    if path.endswith("/") and bare:
        lines.append(f"location = {bare} {{ return 301 {path}; }}")

    lines.append(f"location {path} {{")
    if config.is_static:
        lines += _static_location_lines(config, paths)
    else:
        lines += _proxy_location_lines(config.nginx, port, path)
    lines.append("}")
    return "\n".join(lines) + "\n"


# Mostly-static text, so a template reads straighter than an accumulating
# list of lines. {var_block} is the one dynamic-shaped part: the sorted
# `Environment=` lines plus the optional `EnvironmentFile=` line, pre-joined
# with their own trailing newlines (or "" when there are none) so the
# newline count here matches the old lines.append()-based output exactly.
_UNIT_TEMPLATE = (
    "{header}\n"
    "[Unit]\n"
    "Description={name} (managed by npd)\n"
    "After=network.target\n"
    "StartLimitIntervalSec=0\n"
    "\n"
    "[Service]\n"
    "Type=simple\n"
    "User={user}\n"
    "WorkingDirectory={workdir}\n"
    'Environment="PATH={unit_path}"\n'
    'Environment="PORT={port}"\n'
    "{commit_line}"
    "{var_block}"
    "ExecStart=/bin/bash -c 'exec {start}'\n"
    "Restart=always\n"
    "RestartSec=1\n"
    "{sandbox_block}"
    "\n"
    "[Install]\n"
    "WantedBy=multi-user.target\n"
)


# Opt-in (service.sandbox). The app sees an empty /home apart from its own
# clone, cannot read any app's env file, cannot write outside its clone, and
# cannot take the whole box down with it. Fixed on purpose: npd adds these
# lines, it does not model systemd's sandboxing options. systemd reads
# EnvironmentFile= before applying any of this, so secrets still arrive.
_SANDBOX_TEMPLATE = (
    "ProtectHome=tmpfs\n"
    "BindPaths={clone}\n"
    "InaccessiblePaths=-{env_dir}\n"
    "ProtectSystem=strict\n"
    "PrivateTmp=yes\n"
    "NoNewPrivileges=yes\n"
    "MemoryMax=200M\n"
    "TasksMax=100\n"
    "CPUQuota=50%\n"
)


def render_systemd_unit(
    config: AppConfig, port: int, paths: Paths, commit: str | None = None
) -> str:
    """The systemd unit for one service. Pure: no IO, no subprocess.

    `commit` (when given) is stamped in as NPD_COMMIT, right after PORT.
    There is no state file recording what the running process was built
    from, so the unit itself is the only place that can carry it — without
    it, a build that fails after a `git pull` leaves no way to tell that the
    running code and the checked-out commit have diverged (see
    commands.update)."""
    if config.is_static:
        raise ValueError(f"{config.name} is static and has no unit")
    if config.service is None:
        raise ValueError(f"{config.name} has no [service] section")

    workdir = paths.clone_dir(config.name)
    if config.service.workdir:
        workdir = workdir / config.service.workdir

    commit_line = f'Environment="NPD_COMMIT={commit}"\n' if commit else ""

    env_lines = "".join(
        f'Environment="{key}={_escape_unit_percent(config.env[key])}"\n'
        for key in sorted(config.env)
    )
    env_file_line = (
        f"EnvironmentFile={paths.env_file(config.name)}\n" if config.secrets else ""
    )
    var_block = env_lines + env_file_line

    sandbox_block = (
        _SANDBOX_TEMPLATE.format(
            clone=paths.clone_dir(config.name), env_dir=paths.env
        )
        if config.service.sandbox
        else ""
    )

    start = _escape_unit_percent(config.service.start)
    return _UNIT_TEMPLATE.format(
        header=MANAGED_HEADER,
        name=config.name,
        user=settings.SERVICE_USER,
        workdir=workdir,
        unit_path=settings.UNIT_PATH,
        port=port,
        commit_line=commit_line,
        sandbox_block=sandbox_block,
        var_block=var_block,
        start=start,
    )


def render(
    config: AppConfig, port: int | None, paths: Paths, commit: str | None = None
) -> tuple[Artifact, ...]:
    """Every file this app owns, as a tuple of Artifacts.

    This is the whole of the tool's desired state. Reconcile diffs it against
    disk; nothing else decides what gets written. `commit` is forwarded to
    render_systemd_unit for a service; it has no effect on a static app's
    nginx snippet.
    """
    artifacts: list[Artifact] = []
    if not config.is_static:
        if port is None:
            raise ValueError(f"{config.name} is a service and requires a port")
        artifacts.append(
            Artifact(
                SYSTEMD_UNIT,
                paths.systemd_unit_file(config.name),
                render_systemd_unit(config, port, paths, commit),
            )
        )
    if config.nginx is not None:
        artifacts.append(
            Artifact(
                NGINX_SNIPPET,
                paths.nginx_snippet_file(config.name),
                render_nginx_snippet(config, port, paths),
            )
        )
    return tuple(artifacts)
