"""Coverage bootstrap that runs inside a container (see ``pytest_cov_container.protocol``).

Copied verbatim into each build dir as ``.cov_container/_cov_container_boot.py``;
the injected ``.pth`` imports it and calls :func:`main` at interpreter startup. Standard library
plus the ``coverage`` copy next to it only: the plugin is not installed in the
container. Inert unless both ``COVERAGE_PROCESS_START`` and
``COV_CONTAINER_SINK`` are set, which production never sets.
"""

import atexit
import functools
import importlib.abc
import importlib.util
import os
import signal
import socket
import sys
import threading
import urllib.request
from pathlib import Path

# Mirrors of ``pytest_cov_container.protocol`` (a test keeps them equal).
RCFILE_ENV = "COVERAGE_PROCESS_START"
SINK_ENV = "COV_CONTAINER_SINK"
BOOT_CONFIG = "function.txt"
PID_FILE = "/tmp/.cov_container.pid"  # noqa: S108 — container-side path
# Tests running the boot on a host point the pid file elsewhere.
PID_FILE_ENV = "COV_CONTAINER_PID_FILE"
ID_HEADER = "X-Cov-Id"
FUNCTION_HEADER = "X-Cov-Function"


def _import_coverage(here: str):
    """The ``coverage`` copied next to this module, ahead of any the function bundles."""
    sys.path.insert(0, here)
    try:
        import coverage
    finally:
        sys.path.remove(here)
    return coverage


class _WrappingLoader(importlib.abc.Loader):
    """Delegates to the real loader, then post-processes the executed module."""

    def __init__(self, loader, after_exec):
        self._loader = loader
        self._after_exec = after_exec

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module) -> None:
        self._loader.exec_module(module)
        self._after_exec(module)

    def __getattr__(self, name):
        return getattr(self._loader, name)


class _WrapHandler:
    """Meta-path finder that wraps ``<module>.<attr>`` right after the module loads."""

    def __init__(self, module_name: str, attr: str, after_call):
        self.module_name = module_name
        self.attr = attr
        self.after_call = after_call

    def find_spec(self, name, path, target=None):  # noqa: ARG002 — finder protocol
        if name != self.module_name:
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None:
            return spec
        spec.loader = _WrappingLoader(spec.loader, self._wrap_module)
        return spec

    def _wrap_module(self, module) -> None:
        handler = getattr(module, self.attr, None)
        if callable(handler):
            setattr(module, self.attr, self._wrap(handler))

    def _wrap(self, handler):
        # Sync only: the Lambda Python runtime never awaits an ``async def``
        # handler (it fails to marshal the coroutine), so there is none to wrap.
        after_call = self.after_call

        @functools.wraps(handler)
        def wrapped(*args, **kwargs):
            try:
                return handler(*args, **kwargs)
            finally:
                after_call()

        return wrapped


def _handler_target(handler: str) -> tuple[str, str] | None:
    """``_HANDLER`` (``pkg/mod.func``) as ``("pkg.mod", "func")``; None for a script."""
    module_path, _, attr = handler.rpartition(".")
    if not module_path or not attr or attr == "sh":
        return None
    return module_path.replace("/", "."), attr


def start(rcfile: str, sink: str, here: str) -> None:
    coverage = _import_coverage(here)
    try:
        function = (Path(here) / BOOT_CONFIG).read_text().strip()
    except OSError:
        function = ""

    cov = coverage.Coverage(config_file=rcfile)
    cov.start()
    # A function that bundles coverage also ships its ``a1_coverage.pth``, which
    # would start a second collector from the same env var; this is the guard
    # ``coverage.process_startup`` checks (our ``.pth`` sorts first).
    coverage.process_startup.coverage = cov
    ident = f"{socket.gethostname()}-{os.getpid()}"
    lock = threading.RLock()

    def push() -> None:
        with lock:
            cov.save()
            with open(cov.get_data().data_filename(), "rb") as f:
                body = f.read()
        request = urllib.request.Request(  # noqa: S310  # nosec B310 — the sink URL comes from the plugin
            sink, data=body, method="POST", headers={ID_HEADER: ident, FUNCTION_HEADER: function}
        )
        with urllib.request.urlopen(request, timeout=10):  # noqa: S310  # nosec B310
            pass

    def push_quietly(*_args) -> None:
        try:
            push()
        except Exception as exc:  # noqa: BLE001 — never break the function under test
            print(f"pytest-cov-container: coverage push failed: {exc!r}", file=sys.stderr)  # noqa: T201 — the container's log is the only channel

    Path(os.environ.get(PID_FILE_ENV, PID_FILE)).write_text(str(os.getpid()))
    signal.signal(signal.SIGUSR1, push_quietly)
    atexit.register(push_quietly)
    target = _handler_target(os.environ.get("_HANDLER", ""))
    if target is not None:
        sys.meta_path.insert(0, _WrapHandler(*target, push_quietly))


def main() -> None:
    """Entry point; the injected ``.pth`` calls it at interpreter startup."""
    rcfile = os.environ.get(RCFILE_ENV)
    sink = os.environ.get(SINK_ENV)
    if not rcfile or not sink or not Path(rcfile).is_file():
        return
    start(rcfile, sink, str(Path(__file__).resolve().parent))
