"""The HTTP endpoint containers push their coverage data to (see ``protocol``)."""

import re
import secrets
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pytest_cov_container import protocol

# A data file is SQLite; this caps a runaway or hostile body well above any
# real function's size.
_MAX_BODY = 256 * 1024 * 1024
# The id becomes part of a file name.
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

OnPush = Callable[[str, str, bytes], None]


class CoverageSink:
    """Receives pushes on ``http://<advertise_host>:<port>/<token>``.

    ``on_push(ident, function, body)`` runs on the server's request thread; the
    sink records each accepted push so callers can wait for a container's.
    The random token in the path keeps anything but this session's containers
    from writing data.
    """

    def __init__(self, *, bind: str, advertise_host: str, on_push: OnPush):
        self.token = secrets.token_urlsafe(16)
        self._on_push = on_push
        self._received: dict[str, float] = {}
        self._cond = threading.Condition()
        self._server = ThreadingHTTPServer((bind, 0), self._handler_class())
        self._server.daemon_threads = True
        self.url = f"http://{advertise_host}:{self._server.server_port}/{self.token}"
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_port

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        sink = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != f"/{sink.token}":
                    self.send_error(404)
                    return
                ident = self.headers.get(protocol.ID_HEADER, "")
                function = self.headers.get(protocol.FUNCTION_HEADER, "")
                length = int(self.headers.get("Content-Length") or 0)
                if not _ID_RE.match(ident) or not 0 < length <= _MAX_BODY:
                    self.send_error(400)
                    return
                body = self.rfile.read(length)
                try:
                    sink._on_push(ident, function, body)
                except Exception:  # noqa: BLE001 — report to the container, keep serving
                    self.send_error(500)
                    return
                sink._record(ident)
                self.send_response(204)
                self.end_headers()

            def log_message(self, format, *args):  # noqa: A002 — http.server's signature
                pass

        return Handler

    def _record(self, ident: str) -> None:
        with self._cond:
            self._received[ident] = time.monotonic()
            self._cond.notify_all()

    @property
    def received(self) -> int:
        """Distinct processes that pushed at least once."""
        with self._cond:
            return len(self._received)

    def wait_for(self, container_id: str, *, since: float, timeout: float) -> bool:
        """True once a process in ``container_id`` pushed after ``since`` (``time.monotonic()``)."""
        prefix = f"{container_id[:12]}-"

        def pushed() -> bool:
            return any(ident.startswith(prefix) and at >= since for ident, at in self._received.items())

        with self._cond:
            return self._cond.wait_for(pushed, timeout=timeout)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._server.serve_forever, name="cov-container-sink", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
