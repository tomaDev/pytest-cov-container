import os
import shutil
import tempfile
import threading
import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import coverage
import docker.errors
import pytest
from coverage.data import CoverageData
from coverage.exceptions import CoverageWarning
from coverage.files import PathAliases

import pytest_cov_container
from pytest_cov_container import config as config_module
from pytest_cov_container import inject, ownership, protocol
from pytest_cov_container.config import PluginConfig
from pytest_cov_container.docker_backend import DockerBackend
from pytest_cov_container.frameworks import FunctionTarget
from pytest_cov_container.models import ContainerInfo
from pytest_cov_container.sink import CoverageSink

# How long a flush waits for the signalled containers' pushes. A push is a
# save plus one local HTTP POST; the rest is headroom for a CPU-starved host.
_FLUSH_TIMEOUT_S = 10.0


def _host_branch() -> bool:
    """The host's coverage ``branch`` setting (pytest-cov's running instance, else config)."""
    cov = coverage.Coverage.current()
    if cov is None:
        cov = coverage.Coverage(config_file=True)
    return bool(cov.config.branch)


def _silence_no_data_warning() -> None:
    """Drop coverage's "No data was collected" warning for the rest of this process.

    coverage checks only this process's own data when pytest-cov saves it.
    When the tests only drive containers, that data is empty, yet the
    containers' data (suffix files pytest-cov combines next) is the coverage
    the run exists for. Called only once a container pushed data, so a process
    that got nothing keeps the warning.

    A warnings filter, not ``[run] disable_warnings``: coverage caches that
    list on its first warning, so a later addition can be ignored. Set from
    the ``pytest_runtestloop`` wrapper, not where data arrives: a fixture
    teardown runs inside pytest's per-test ``catch_warnings``, which would undo it.
    """
    warnings.filterwarnings("ignore", message="No data was collected", category=CoverageWarning)


class ContainerCovPlugin:
    """Inject before the session; receive container pushes during it.

    Every process that runs tests (each xdist worker, or a plain run) serves a
    sink; ``container_env()`` points the containers it starts at that sink, so
    a process receives exactly its own containers' data. Each push becomes a
    suffix file of pytest-cov's data file
    (``<data_file>.container-<worker>-<container>-<pid>``), mapped to host
    source paths, which pytest-cov's own combine merges with the host data.
    """

    def __init__(
        self,
        plugin_config: PluginConfig,
        rootpath: Path,
        *,
        invocation_dir: Path | None = None,
        required: bool = False,
        is_worker: bool = False,
    ):
        if not plugin_config.targets:
            msg = "PluginConfig.targets is empty"
            raise ValueError(msg)
        container_roots = {target.container_root for target in plugin_config.targets}
        if len(container_roots) > 1:
            # container_env() is one dict shared by every container this
            # process starts, so RCFILE_ENV can only encode one container_root.
            msg = f"PluginConfig.targets have different container_root values: {sorted(container_roots)}"
            raise ValueError(msg)
        self.config = plugin_config
        self.rootpath = rootpath
        self.invocation_dir = invocation_dir or rootpath
        self.required = required or plugin_config.required
        self.is_worker = is_worker
        self.backend = DockerBackend()
        self.sink: CoverageSink | None = None
        self.targets = {target.name: target for target in plugin_config.targets}
        self.coverage_dir = Path(tempfile.mkdtemp(prefix="cov_container_"))
        self.shards: dict[str, Path] = {}
        self._shards_lock = threading.Lock()
        self._mappers: dict[str, PathAliases] = {}
        self.explicit_calls = 0
        self.failure: str | None = None

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session):
        # tryfirst: under xdist the controller must inject before its
        # ``dsession`` spawns workers (whose containers read the files).
        if not self.is_worker:
            branch = _host_branch() if self.config.branch is None else self.config.branch
            for target in self.config.targets:
                inject.inject(target, rootpath=session.config.rootpath, branch=branch)
        # The xdist controller runs no tests, so it starts no containers.
        if session.config.pluginmanager.hasplugin("dsession"):
            return
        bind, advertise = self._sink_address()
        self.sink = CoverageSink(bind=bind, advertise_host=advertise, on_push=self._on_push)
        self.sink.start()

    def _sink_address(self) -> tuple[str, str]:
        """``(bind, advertise)``: the configured values, else what the Docker engine needs."""
        bind, advertise = self.config.sink_bind, self.config.sink_host
        if bind and advertise:
            return bind, advertise
        try:
            detected_bind, detected_advertise = self.backend.host_endpoint(self.config.docker_network)
        except docker.errors.DockerException as exc:
            # No engine to ask: the session's own docker checks report that
            # better than a crash here, and containers cannot start anyway.
            warnings.warn(
                f"pytest-cov-container: could not detect the Docker engine ({exc}); "
                "assuming Docker Desktop. Set sink_bind and sink_host to override.",
                UserWarning,
                stacklevel=2,
            )
            detected_bind, detected_advertise = "127.0.0.1", "host.docker.internal"
        return bind or detected_bind, advertise or detected_advertise

    # trylast → innermost wrapper: this runs after the tests (and the session
    # fixtures' teardowns) and before pytest-cov's wrapper combines and reports.
    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_runtestloop(self, session):
        result = yield
        if self.sink is not None:
            self._collect_at_end(session)
            self.sink.stop()
        if self.shards:
            _silence_no_data_warning()
        return result

    def pytest_terminal_summary(self, terminalreporter):
        if self.failure:
            terminalreporter.section("pytest-cov-container", red=True, bold=True)
            terminalreporter.line(self.failure, red=True)

    def pytest_unconfigure(self, config):  # noqa: ARG002
        shutil.rmtree(self.coverage_dir, ignore_errors=True)
        pytest_cov_container._register_active_plugin(None)

    def container_env(self) -> dict[str, str]:
        """Env the containers need: the coverage bootstrap vars plus this worker's marker."""
        env: dict[str, str] = {}
        if self.sink is not None:
            container_root = self.config.targets[0].container_root.rstrip("/")
            env[protocol.RCFILE_ENV] = f"{container_root}/{protocol.RCFILE}"
            env[protocol.SINK_ENV] = self.sink.url
        if self.config.worker_env:
            env[self.config.worker_env] = ownership.worker_id()
        return env

    def find_owned(self, *, all_workers: bool = False) -> list[ContainerInfo]:
        return ownership.find_owned(self.backend, self.config, self.rootpath, all_workers=all_workers)

    def collect_from_running(self) -> int:
        """Ask every running container this process owns to push its coverage now.

        Call it before stopping the containers of a long-running server (a
        per-invocation handler pushes by itself). Returns the number of
        containers that pushed. With ``required``, a process that received no
        data at all raises.
        """
        self.explicit_calls += 1
        _, pushed = self._flush()
        if self.required and not self.shards:
            msg = f"pytest-cov-container: no coverage received {self._ownership_desc()}. {self._hint()}"
            raise RuntimeError(msg)
        return pushed

    def _flush(self) -> tuple[int, int]:
        """Signal the running owned containers, then wait for their pushes.

        Returns ``(signalled, pushed)``: containers with a coverage process, and
        those whose push arrived in time.
        """
        if self.sink is None:
            return 0, 0
        running = [c for c in self.find_owned() if c.status == "running"]
        since = time.monotonic()
        signalled = self._signal(running)
        deadline = since + _FLUSH_TIMEOUT_S
        pushed = 0
        for container in signalled:
            if self.sink.wait_for(container.id, since=since, timeout=max(0.0, deadline - time.monotonic())):
                pushed += 1
            else:
                warnings.warn(
                    f"pytest-cov-container: {container.name} ({container.id[:12]}) did not push its "
                    f"coverage within {_FLUSH_TIMEOUT_S:.0f}s of SIGUSR1",
                    UserWarning,
                    stacklevel=3,
                )
        return len(signalled), pushed

    def _signal(self, containers: list[ContainerInfo]) -> list[ContainerInfo]:
        """SIGUSR1 the containers on a small thread pool; returns those signalled.

        Each signal is a ``docker exec`` round-trip; fanning them out cuts the
        wait from O(N * RTT) to ~RTT. A container that fails is warned (here, on
        the calling thread, where pytest captures warnings) and does not block
        the rest. One without a coverage process is skipped quietly: a
        framework label also matches containers of functions it does not
        instrument (a non-Python runtime), and ``required`` catches a run
        where no container pushed.
        """
        if not containers:
            return []
        with ThreadPoolExecutor(max_workers=min(8, len(containers))) as ex:
            futures = [(c, ex.submit(self.backend.send_signal, c.id)) for c in containers]
        signalled: list[ContainerInfo] = []
        for container, future in futures:
            try:
                if future.result():
                    signalled.append(container)
            except Exception as exc:  # noqa: BLE001 — surface, don't crash
                warnings.warn(
                    f"pytest-cov-container: could not signal {container.name} ({container.id[:12]}): {exc}",
                    UserWarning,
                    stacklevel=4,
                )
        return signalled

    def _ownership_desc(self) -> str:
        parts = [f"matching image {self.config.image_patterns or 'any'}"]
        if self.config.label:
            parts.append(f"labelled {self.config.label}")
        if self.config.mount_prefix:
            parts.append(f"mounting {self.rootpath / self.config.mount_prefix}")
        if self.config.worker_env:
            parts.append(f"with {self.config.worker_env}={ownership.worker_id()}")
        return "from containers " + ", ".join(parts)

    def _hint(self) -> str:
        return (
            f"Check that the containers get container_env() ({protocol.RCFILE_ENV} and {protocol.SINK_ENV} "
            f"declared in the template), and that they can reach {self.sink.url if self.sink else 'the sink'}."
        )

    def _collect_at_end(self, session) -> None:
        """Flush containers still running, then enforce ``required``."""
        signalled, _ = self._flush()
        if not self.required or self.shards or (self.explicit_calls == 0 and signalled == 0):
            return
        msg = f"pytest-cov-container: no coverage received {self._ownership_desc()}. {self._hint()}"
        if self.is_worker:
            # A worker's testsfailed never reaches the controller's exit code.
            raise RuntimeError(msg)
        self.failure = msg
        session.testsfailed += 1

    def _data_file_base(self) -> Path:
        """pytest-cov's data file (``COVERAGE_FILE`` / ``[run] data_file``), absolute."""
        cov = coverage.Coverage.current()
        name = cov.config.data_file if cov else os.environ.get("COVERAGE_FILE", ".coverage")
        path = Path(name)
        return path if path.is_absolute() else self.invocation_dir / path

    def _mapper(self, target: FunctionTarget) -> PathAliases:
        mapper = self._mappers.get(target.name)
        if mapper is None:
            mapper = PathAliases()
            for mapping in target.mappings:
                host = Path(mapping.host_dir)
                mapper.add(mapping.container_dir, str(host if host.is_absolute() else self.rootpath / host))
            self._mappers[target.name] = mapper
        return mapper

    def _on_push(self, ident: str, function: str, body: bytes) -> None:
        """One container process's cumulative data, as a suffix file of pytest-cov's data file.

        Runs on the sink's request thread. The latest push of a process
        replaces its earlier ones (same shard name). Written under a temp name
        pytest-cov's combine glob (``<data_file>.*``) cannot match, then renamed.
        """
        target = self.targets.get(function)
        if target is None:
            msg = f"push from unknown function {function!r}"
            raise ValueError(msg)
        base = self._data_file_base()
        shard = base.with_name(f"{base.name}.container-{ownership.worker_id()}-{ident}")
        received = self.coverage_dir / f"{uuid.uuid4().hex}.in"
        tmp = base.with_name(f".cov-container-{uuid.uuid4().hex}.tmp")
        received.write_bytes(body)
        try:
            src = CoverageData(basename=str(received))
            src.read()
            dst = CoverageData(basename=str(tmp))
            with self._shards_lock:
                dst.update(src, map_path=self._mapper(target).map)
            dst.close()
            src.close()
            tmp.replace(shard)
        finally:
            received.unlink(missing_ok=True)
            tmp.unlink(missing_ok=True)
        with self._shards_lock:
            self.shards[ident] = shard


def pytest_addoption(parser):
    group = parser.getgroup("cov-container")
    group.addoption(
        "--no-cov-container",
        action="store_true",
        default=False,
        help="Disable container coverage collection",
    )
    group.addoption(
        "--cov-container-required",
        action="store_true",
        default=False,
        help="Fail when a process that flushed containers received no container coverage data",
    )


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    if config.getoption("--no-cov-container", default=False):
        return

    if not config.pluginmanager.hasplugin("pytest_cov"):
        return

    cov_sources = config.getoption("--cov", default=[])
    if not cov_sources or config.getoption("--no-cov", default=False):
        return

    plugin_config = config_module.load_config(config.rootpath / "pyproject.toml")
    if plugin_config is None or not plugin_config.enabled:
        return

    plugin = ContainerCovPlugin(
        plugin_config,
        config.rootpath,
        invocation_dir=config.invocation_params.dir,
        required=config.getoption("--cov-container-required", default=False),
        is_worker=hasattr(config, "workerinput"),
    )
    # Not "cov_container": that name is taken by this module itself (the
    # pytest11 entry point), and a duplicate name raises at configure.
    config.pluginmanager.register(plugin, "cov_container_session")
    pytest_cov_container._register_active_plugin(plugin)
