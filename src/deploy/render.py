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
