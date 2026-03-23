import subprocess
import tempfile
import warnings
from pathlib import Path

import pytest

import pytest_cov_container
from pytest_cov_container import config as config_module
from pytest_cov_container import drivers
from pytest_cov_container.docker_backend import DockerBackend
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

        self.injection_result = self.driver.inject(target_dir, self.config.driver_config)

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

        for container in containers:
            self.driver.collect(self.backend, container, self.coverage_dir, self.config.driver_config)

        self._combine_coverage(session.config.rootpath)

    def collect_from_running(self):
        containers = self.backend.find_containers(
            image_pattern=self.config.image_pattern,
            label=self.config.label,
        )
        running = [c for c in containers if c.status == "running"]
        for container in running:
            self.driver.collect(self.backend, container, self.coverage_dir, self.config.driver_config)

    def _combine_coverage(self, root: Path):
        rc_path = self.coverage_dir / ".coveragerc"
        lines = ["[paths]", "source ="]
        for host_path, container_path in self.config.path_mapping.items():
            lines.append(f"    {host_path}")
            lines.append(f"    {container_path}")
        rc_path.write_text("\n".join(lines) + "\n")

        result = subprocess.run(
            ["coverage", "combine", f"--rcfile={rc_path}", str(self.coverage_dir)],  # noqa: S607
            check=False,
            cwd=root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            warnings.warn(
                f"coverage combine failed: {result.stderr}",
                UserWarning,
                stacklevel=2,
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
    pytest_cov_container._active_plugin = plugin  # noqa: SLF001
