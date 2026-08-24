from __future__ import annotations

import http.client
import socket
import threading
from http.server import BaseHTTPRequestHandler

from peerbridge_mcp.http_limits import BoundedThreadingHTTPServer


class _BlockingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        self.server.started.set()  # type: ignore[attr-defined]
        self.server.release.wait(timeout=5)  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()


class _OneWorkerServer(BoundedThreadingHTTPServer):
    max_request_workers = 1


def test_bounded_http_server_returns_503_when_all_handlers_are_busy() -> None:
    server = _OneWorkerServer(("127.0.0.1", 0), _BlockingHandler)
    server.started = threading.Event()  # type: ignore[attr-defined]
    server.release = threading.Event()  # type: ignore[attr-defined]
    runner = threading.Thread(target=server.serve_forever, daemon=True)
    runner.start()
    first = socket.create_connection(server.server_address, timeout=5)
    try:
        first.sendall(b"GET /hold HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        assert server.started.wait(timeout=5)  # type: ignore[attr-defined]
        second = http.client.HTTPConnection(*server.server_address, timeout=5)
        try:
            second.request("GET", "/overloaded")
            response = second.getresponse()
            assert response.status == 503
            assert response.getheader("Connection") == "close"
            assert response.read() == b""
        finally:
            second.close()
    finally:
        server.release.set()  # type: ignore[attr-defined]
        try:
            first.recv(4096)
        except OSError:
            pass
        first.close()
        server.shutdown()
        server.server_close()
        runner.join(timeout=5)
        assert not runner.is_alive()
