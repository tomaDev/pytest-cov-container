import os
import shutil
import tempfile
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import coverage
import pytest
from coverage.data import CoverageData
from coverage.exceptions import CoverageWarning
from coverage.files import PathAliases

import pytest_cov_container
from pytest_cov_container import config as config_module
from pytest_cov_container import drivers, ownership
from pytest_cov_container.config import PluginConfig
from pytest_cov_container.docker_backend import DockerBackend
from pytest_cov_container.models import ContainerInfo
from pytest_cov_container.resolver import SamBuildResolver


def _host_branch() -> bool:
    """The host's coverage ``branch`` setting (pytest-cov's running instance, else config)."""
    cov = coverage.Coverage.current()
    if cov is None:
        cov = coverage.Coverage(config_file=True)
    return bool(cov.config.branch)


def _silence_no_data_warning() -> None:
    """Drop coverage's "No data was collected" warning for the rest of this process.

    coverage checks only this process's own data when pytest-cov saves it.
    When the tests only drive a container, that data is empty, yet the
    container's data (suffix files pytest-cov combines next) is the coverage
    the run exists for. Called only once a container yielded data, so a
    process that got nothing from its containers keeps the warning.

    A warnings filter, not ``[run] disable_warnings``: coverage caches that
    list on its first warning, so a later addition can be ignored. Set from
    the ``pytest_runtestloop`` wrapper, not where data is collected: a fixture
    teardown runs inside pytest's per-test ``catch_warnings``, which would undo it.
    """
    warnings.filterwarnings("ignore", message="No data was collected", category=CoverageWarning)


class ContainerCovPlugin:
    """Inject before the session, collect container coverage after it.

    Collected data files become suffix files of pytest-cov's data file
    (``<data_file>.container-<worker>-<id>``). pytest-cov's own combine then
    merges them with the host data under the project's coverage config
    (``[paths]``, ``branch``), and its report and ``--cov-fail-under`` include
    them. Under pytest-xdist the controller injects (before any worker starts),
    each worker collects only the containers it owns, and pytest-cov's
    controller combines everything once.
    """

    def __init__(
        self,
        plugin_config: PluginConfig,
        rootpath: Path,
        *,
        invocation_dir: Path | None = None,
        required: bool = False,
        is_worker: bool = False,
        is_controller: bool = False,
    ):
        if plugin_config.driver_config is None:
            raise ValueError("PluginConfig.driver_config is required")
        self.config = plugin_config
        self.driver_config = plugin_config.driver_config
        self.rootpath = rootpath
        self.invocation_dir = invocation_dir or rootpath
        self.required = required or plugin_config.required
        self.is_worker = is_worker
        self.is_controller = is_controller
        self.backend = DockerBackend()
        self.driver = drivers.get_driver(plugin_config.language)
        self.resolver = SamBuildResolver()
        self.injection_result = None
        self.coverage_dir = Path(tempfile.mkdtemp(prefix="cov_container_"))
        self.collected_ids: set[str] = set()
        self.files_collected = 0
        self.explicit_calls = 0
        self.failure: str | None = None

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionstart(self, session):
        # tryfirst: under xdist the controller must inject before its
        # ``dsession`` spawns workers (whose containers read the files).
        if self.is_worker:
            return
        target_dir = self.resolver.resolve_target_dir(
            self.driver_config.build_dir,
            session.config.rootpath,
        )
        if not target_dir.exists():
            msg = f"Build directory {target_dir} does not exist. Run 'sam build' before running tests."
            raise FileNotFoundError(msg)

        self.injection_result = self.driver.inject(
            target_dir,
            self.driver_config,
            rootpath=session.config.rootpath,
            branch=_host_branch(),
        )

    # trylast → innermost wrapper: this runs after the tests and before
    # pytest-cov's wrapper stops coverage, combines and reports.
    @pytest.hookimpl(wrapper=True, trylast=True)
    def pytest_runtestloop(self, session):
        result = yield
        if not self.is_controller:
            self._collect_at_end(session)
        if self.files_collected:
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
        """Env the container needs: the coverage bootstrap vars plus this worker's marker."""
        env = self.driver.container_env(self.driver_config)
        if self.config.worker_env:
            env[self.config.worker_env] = ownership.worker_id()
        return env

    def find_owned(self, *, all_workers: bool = False) -> list[ContainerInfo]:
        return ownership.find_owned(
            self.backend, self.config, self.rootpath, all_workers=all_workers
        )

    def collect_from_running(self) -> int:
        """Save and collect every running container this process owns.

        Returns the number of data files collected. With ``required``, zero
        raises: call this in the teardown of the fixture that stops the
        containers, and a missing container fails that teardown loudly.
        """
        self.explicit_calls += 1
        running = [c for c in self.find_owned() if c.status == "running"]
        collected = self._collect(running)
        if collected == 0 and self.required:
            raise RuntimeError(
                "pytest-cov-container: no coverage collected from "
                f"{len(running)} running container(s) {self._ownership_desc()}. "
                "Check that the container started, has the save protocol's pid "
                "file, and was not removed before collection (`docker ps -a`)."
            )
        return collected

    def _ownership_desc(self) -> str:
        parts = [f"matching image {self.config.image_patterns or 'any'}"]
        if self.config.label:
            parts.append(f"labelled {self.config.label}")
        if self.config.mount_prefix:
            parts.append(f"mounting {self.rootpath / self.config.mount_prefix}")
        if self.config.worker_env:
            parts.append(f"with {self.config.worker_env}={ownership.worker_id()}")
        return ", ".join(parts)

    def _collect_at_end(self, session) -> None:
        """Collect owned containers the session left behind (any state).

        Containers already collected by an explicit call are skipped. When the
        session made an explicit call, finding nothing more here is normal.
        """
        pending = [c for c in self.find_owned() if c.id not in self.collected_ids]
        if not pending:
            if self.explicit_calls == 0:
                warnings.warn(
                    f"No matching containers found for coverage collection ({self._ownership_desc()})",
                    UserWarning,
                    stacklevel=2,
                )
            return
        if self._collect(pending) > 0 or not self.required:
            return
        msg = (
            f"pytest-cov-container: {len(pending)} container(s) "
            f"{self._ownership_desc()} yielded no coverage data."
        )
        if self.is_worker:
            # A worker's testsfailed never reaches the controller's exit code.
            raise RuntimeError(msg)
        self.failure = msg
        session.testsfailed += 1

    def _collect(self, containers: list[ContainerInfo]) -> int:
        """Run ``driver.collect`` per container on a small thread pool, then hand
        the data files to pytest-cov. Returns the number of data files.

        Each collect call does docker round-trips (signal, sentinel poll,
        archive); fanning them out cuts the wait from O(N * RTT) to ~RTT.
        One container's failure is warned and does not block the rest.
        """
        if not containers:
            return 0
        files: list[Path] = []
        cfg = self.driver_config
        with ThreadPoolExecutor(max_workers=min(8, len(containers))) as ex:
            future_to_container = {
                ex.submit(
                    self.driver.collect, self.backend, container, self.coverage_dir, cfg
                ): container
                for container in containers
            }
            for future in as_completed(future_to_container):
                container = future_to_container[future]
                self.collected_ids.add(container.id)
                try:
                    files.extend(future.result())
                except Exception as exc:  # noqa: BLE001 — surface, don't crash
                    warnings.warn(
                        f"collect failed for container {container.name} "
                        f"({container.id[:12]}): {exc}",
                        UserWarning,
                        stacklevel=2,
                    )
        for path in files:
            self._write_shard(path)
        self.files_collected += len(files)
        return len(files)

    def _data_file_base(self) -> Path:
        """pytest-cov's data file (``COVERAGE_FILE`` / ``[run] data_file``), absolute."""
        cov = coverage.Coverage.current()
        name = cov.config.data_file if cov else os.environ.get("COVERAGE_FILE", ".coverage")
        path = Path(name)
        return path if path.is_absolute() else self.invocation_dir / path

    def _path_mapper(self):
        if not self.config.path_mapping:
            return lambda path: path
        aliases = PathAliases()
        for host_path, container_path in self.config.path_mapping.items():
            host = Path(host_path)
            aliases.add(container_path, str(host if host.is_absolute() else self.rootpath / host))
        return aliases.map

    def _write_shard(self, source: Path) -> None:
        """Rewrite one container data file as a suffix file of pytest-cov's data file.

        Written under a temp name that pytest-cov's combine glob
        (``<data_file>.*``) cannot match, then renamed into place.
        """
        base = self._data_file_base()
        shard = base.with_name(
            f"{base.name}.container-{ownership.worker_id()}-{uuid.uuid4().hex[:12]}"
        )
        tmp = base.with_name(f".cov-container-{uuid.uuid4().hex}.tmp")
        src = CoverageData(basename=str(source))
        src.read()
        dst = CoverageData(basename=str(tmp))
        dst.update(src, map_path=self._path_mapper())
        dst.close()
        src.close()
        tmp.replace(shard)
        source.unlink(missing_ok=True)


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
        help="Fail when a collection pass gets no container coverage data",
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
        is_controller=config.pluginmanager.hasplugin("dsession"),
    )
    # Not "cov_container": that name is taken by this module itself (the
    # pytest11 entry point), and a duplicate name raises at configure.
    config.pluginmanager.register(plugin, "cov_container_session")
    pytest_cov_container._register_active_plugin(plugin)
