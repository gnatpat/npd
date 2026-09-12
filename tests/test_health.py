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
