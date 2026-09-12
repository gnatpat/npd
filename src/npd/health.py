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
