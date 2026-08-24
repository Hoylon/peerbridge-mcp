"""Bounded request concurrency for PeerBridge's local HTTP control planes."""

from __future__ import annotations

import socket
import threading
from http.server import ThreadingHTTPServer
from typing import Any


DEFAULT_MAX_REQUEST_WORKERS = 24
DEFAULT_REQUEST_TIMEOUT_SECONDS = 10.0
_OVERLOADED_RESPONSE = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Content-Length: 0\r\n"
    b"Cache-Control: no-store\r\n"
    b"Connection: close\r\n"
    b"\r\n"
)


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server with a hard cap on live request handlers."""

    daemon_threads = True
    max_request_workers = DEFAULT_MAX_REQUEST_WORKERS
    request_timeout_seconds = DEFAULT_REQUEST_TIMEOUT_SECONDS

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        workers = int(self.max_request_workers)
        if workers < 1:
            raise ValueError("max_request_workers must be positive")
        self._request_slots = threading.BoundedSemaphore(workers)
        super().__init__(*args, **kwargs)

    def get_request(self) -> tuple[socket.socket, Any]:
        request, client_address = super().get_request()
        request.settimeout(float(self.request_timeout_seconds))
        return request, client_address

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            try:
                request.sendall(_OVERLOADED_RESPONSE)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._request_slots.release()
            raise

    def process_request_thread(
        self, request: socket.socket, client_address: Any
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()
