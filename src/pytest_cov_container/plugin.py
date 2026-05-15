import subprocess
import sys
import tempfile
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pytest

import pytest_cov_container
from pytest_cov_container import config as config_module
from pytest_cov_container import drivers
from pytest_cov_container.docker_backend import DockerBackend
from pytest_cov_container.models import ContainerInfo
from pytest_cov_container.resolver import SamBuildResolver


class ContainerCovPlugin:
    def __init__(self, plugin_config):
        self.config = plugin_config
        self.backend = DockerBackend()
        self.driver = drivers.get_driver(plugin_config.language)
        self.resolver = SamBuildResolver()
        self.injection_result = None
        self.coverage_dir = Path(tempfile.mkdtemp(prefix="cov_container_"))

    def pytest_sessionstart(self, session):
        target_dir = self.resolver.resolve_target_dir(
            self.config.driver_config.build_dir,
            session.config.rootpath,
        )
        if not target_dir.exists():
            msg = f"Build directory {target_dir} does not exist. Run 'sam build' before running tests."
            raise FileNotFoundError(msg)

        self.injection_result = self.driver.inject(
            target_dir, self.config.driver_config
        )

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):  # noqa: ARG002
        containers = self.backend.find_containers(
            image_pattern=self.config.image_pattern,
            label=self.config.label,
        )

        if not containers:
            warnings.warn(
                "No matching containers found for coverage collection",
                UserWarning,
                stacklevel=2,
            )

        self._collect_concurrently(containers)
        self._combine_coverage(session.config.rootpath)

    def _collect_concurrently(self, containers: list[ContainerInfo]) -> None:
        """Run ``driver.collect`` per container on a small thread pool. Each
        collect call does at least one round-trip to the docker daemon
        (and the wait-for-save poll); fanning them out cuts session-finish
        from O(N * RTT) to ~RTT for typical N. Failures in one container
        don't block the rest — collect errors surface as warnings.
        """
        if not containers:
            return
        max_workers = min(8, len(containers))
        cfg = self.config.driver_config
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_to_container = {
                ex.submit(
                    self.driver.collect,
                    self.backend,
                    container,
                    self.coverage_dir,
                    cfg,
                ): container
                for container in containers
            }
            for future in as_completed(future_to_container):
                container = future_to_container[future]
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 — surface, don't crash
                    warnings.warn(
                        f"collect failed for container {container.name} "
                        f"({container.id[:12]}): {exc}",
                        UserWarning,
                        stacklevel=2,
                    )

    def collect_from_running(self):
        containers = self.backend.find_containers(
            image_pattern=self.config.image_pattern,
            label=self.config.label,
        )
        running = [c for c in containers if c.status == "running"]
        for container in running:
            self.driver.collect(
                self.backend, container, self.coverage_dir, self.config.driver_config
            )

    def _combine_coverage(self, root: Path):
        rc_path = self.coverage_dir / ".coveragerc"
        lines = ["[paths]"]
        for i, (host_path, container_path) in enumerate(
            self.config.path_mapping.items()
        ):
            label = "source" if i == 0 else f"source{i}"
            lines.append(f"{label} =")
            lines.append(f"    {host_path}")
            lines.append(f"    {container_path}")
        rc_path.write_text("\n".join(lines) + "\n")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "coverage",
                "combine",
                f"--rcfile={rc_path}",
                str(self.coverage_dir),
            ],
            check=False,
            cwd=root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            detail = stderr or stdout or "(no diagnostic output)"
            raise RuntimeError(
                f"coverage combine failed (rc={result.returncode}): {detail}"
            )


def pytest_addoption(parser):
    group = parser.getgroup("cov-container")
    group.addoption(
        "--no-cov-container",
        action="store_true",
        default=False,
        help="Disable container coverage collection",
    )


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    if config.getoption("--no-cov-container", default=False):
        return

    if not config.pluginmanager.hasplugin("pytest_cov"):
        return

    cov_sources = config.getoption("--cov", default=[])
    if not cov_sources:
        return

    plugin_config = config_module.load_config(config.rootpath / "pyproject.toml")
    if plugin_config is None or not plugin_config.enabled:
        return

    plugin = ContainerCovPlugin(plugin_config)
    config.pluginmanager.register(plugin, "cov_container")
    pytest_cov_container._register_active_plugin(plugin)
