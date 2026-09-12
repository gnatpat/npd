"""A local reverse proxy that reproduces, for `deploy dev --prefix`, the
prefix-stripping and header-forwarding behaviour that
`render.render_nginx_snippet` generates for production nginx — so prefix bugs
(e.g. an app that assumes it owns `/`) surface on the laptop instead of on
first deploy.

Deliberate limitations:
- The whole upstream response is buffered in memory before it is forwarded,
  so streaming responses and Server-Sent Events will not work through
  `--prefix` (the app still runs fine without `--prefix`).
- This exists only to mirror the generated nginx snippet's prefix and
  forwarded-header behaviour; it is not a general-purpose proxy.
"""

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

            if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
                # Real chunked decoding is out of scope for a dev proxy;
                # failing clearly beats silently dropping the body and
                # corrupting the next request on this keep-alive connection.
                self.send_error(
                    411,
                    "chunked request bodies are not supported by the dev proxy",
                )
                return

            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else 0
                if length < 0:
                    raise ValueError("negative Content-Length")
            except ValueError:
                self.send_error(400, "invalid Content-Length")
                return

            body = self.rfile.read(length) if length else None

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

            conn = http.client.HTTPConnection("127.0.0.1", upstream_port)
            try:
                conn.request(self.command, decision.path, body=body, headers=headers)
                upstream = conn.getresponse()
                payload = upstream.read()
            except (OSError, http.client.HTTPException) as exc:
                self.send_error(502, f"upstream unreachable: {exc}")
                return
            finally:
                conn.close()

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
