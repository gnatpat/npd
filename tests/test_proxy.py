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


# --- Fix round 1: defensive-handling tests -----------------------------

import socket

from deploy.proxy import _handler


def _raw_request(port: int, request: bytes) -> bytes:
    """Send a hand-built HTTP request and return whatever comes back. Used
    where urllib would refuse to send a malformed request itself (bad
    Content-Length, chunked without real chunk framing)."""
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(request)
        sock.settimeout(5)
        return sock.recv(65536)


def _start_broken_upstream() -> int:
    """A stub upstream that accepts one connection, sends a response that
    lies about its own length, then closes early -- provoking
    http.client.IncompleteRead, which is not an OSError."""
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.bind(("127.0.0.1", 0))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]

    def handle():
        conn, _ = server_sock.accept()
        try:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nshort")
        finally:
            conn.close()
            server_sock.close()

    threading.Thread(target=handle, daemon=True).start()
    return port


def test_malformed_content_length_gets_400_not_a_reset():
    nginx = NginxConfig(path="/pokemon/", strip_prefix=False)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), _handler(nginx, 1))
    proxy_port = _start(proxy)

    request = (
        b"POST /pokemon/x HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Length: notanumber\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    response = _raw_request(proxy_port, request)
    assert response.startswith(b"HTTP/1.1 400")

    proxy.shutdown()


def test_chunked_request_body_gets_411():
    nginx = NginxConfig(path="/pokemon/", strip_prefix=False)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), _handler(nginx, 1))
    proxy_port = _start(proxy)

    request = (
        b"POST /pokemon/x HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"Connection: close\r\n"
        b"\r\n"
        b"0\r\n\r\n"
    )
    response = _raw_request(proxy_port, request)
    assert response.startswith(b"HTTP/1.1 411")

    proxy.shutdown()


def test_a_broken_upstream_produces_502_not_a_traceback():
    upstream_port = _start_broken_upstream()

    nginx = NginxConfig(path="/pokemon/", strip_prefix=False)
    proxy = ThreadingHTTPServer(("127.0.0.1", 0), _handler(nginx, upstream_port))
    proxy_port = _start(proxy)

    request = b"GET /pokemon/cards HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
    response = _raw_request(proxy_port, request)
    assert response.startswith(b"HTTP/1.1 502")

    proxy.shutdown()
