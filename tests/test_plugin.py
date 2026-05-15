import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pytest_cov_container.config import PluginConfig
from pytest_cov_container.drivers.python import PythonDriver
from pytest_cov_container.models import ContainerInfo, DriverConfig
from pytest_cov_container.plugin import ContainerCovPlugin


@pytest.fixture
def plugin_config():
    return PluginConfig(
        image_pattern="samcli/lambda*",
        label="pytest-cov-container",
        language="python",
        enabled=True,
        path_mapping={"src/api": "/var/task"},
        driver_config=DriverConfig(
            build_dir=".aws-sam/build/ApiFunction",
            entrypoint="uvicorn app:app --host 0.0.0.0 --port 8080",
            path_mapping={"src/api": "/var/task"},
        ),
    )


class TestContainerCovPluginSessionStart:
    @patch("pytest_cov_container.plugin.DockerBackend")
    def test_injects_when_build_dir_exists(
        self, mock_backend_cls, plugin_config, tmp_path
    ):  # noqa: ARG002
        build_dir = tmp_path / ".aws-sam" / "build" / "ApiFunction"
        build_dir.mkdir(parents=True)
        plugin_config.driver_config.build_dir = str(build_dir)

        plugin = ContainerCovPlugin(plugin_config)
        mock_session = MagicMock()
        mock_session.config.rootpath = tmp_path

        plugin.pytest_sessionstart(mock_session)

        assert plugin.injection_result is not None
        assert (build_dir / ".coveragerc").exists()
        assert (build_dir / "_cov_wrapper.py").exists()
        assert (build_dir / "run.sh").exists()

    @patch("pytest_cov_container.plugin.DockerBackend")
    def test_raises_when_build_dir_missing(
        self, mock_backend_cls, plugin_config, tmp_path
    ):  # noqa: ARG002
        plugin = ContainerCovPlugin(plugin_config)
        mock_session = MagicMock()
        mock_session.config.rootpath = tmp_path

        with pytest.raises(FileNotFoundError, match="does not exist"):
            plugin.pytest_sessionstart(mock_session)


class TestContainerCovPluginSessionFinish:
    @patch("pytest_cov_container.plugin.DockerBackend")
    @patch("subprocess.run")
    def test_warns_when_no_containers_found(
        self, mock_subprocess, mock_backend_cls, plugin_config
    ):  # noqa: ARG002
        plugin = ContainerCovPlugin(plugin_config)
        plugin.backend.find_containers.return_value = []

        mock_session = MagicMock()
        mock_session.config.rootpath = Path("/project")
        with pytest.warns(UserWarning, match="No matching containers"):
            plugin.pytest_sessionfinish(mock_session, exitstatus=0)

    @patch("pytest_cov_container.plugin.DockerBackend")
    @patch("subprocess.run")
    def test_collects_and_combines(
        self, mock_subprocess, mock_backend_cls, plugin_config
    ):  # noqa: ARG002
        plugin = ContainerCovPlugin(plugin_config)
        container = ContainerInfo(
            id="abc123",
            name="test",
            image="samcli/lambda:3.12",
            labels={},
            status="exited",
        )
        plugin.backend.find_containers.return_value = [container]
        plugin.backend.extract_matching_files.return_value = []

        mock_session = MagicMock()
        mock_session.config.rootpath = Path("/project")

        with pytest.warns(UserWarning, match="coverage combine failed"):
            plugin.pytest_sessionfinish(mock_session, exitstatus=0)

        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        cmd = call_args[0][0]
        assert cmd[1:4] == ["-m", "coverage", "combine"]

    @patch("pytest_cov_container.plugin.DockerBackend")
    @patch("subprocess.run")
    def test_writes_paths_config(
        self, mock_subprocess, mock_backend_cls, plugin_config
    ):  # noqa: ARG002
        plugin = ContainerCovPlugin(plugin_config)
        container = ContainerInfo(
            id="abc123",
            name="test",
            image="samcli/lambda:3.12",
            labels={},
            status="exited",
        )
        plugin.backend.find_containers.return_value = [container]
        plugin.backend.extract_matching_files.return_value = []

        mock_session = MagicMock()
        mock_session.config.rootpath = Path("/project")

        with pytest.warns(UserWarning, match="coverage combine failed"):
            plugin.pytest_sessionfinish(mock_session, exitstatus=0)

        rc_path = plugin.coverage_dir / ".coveragerc"
        assert rc_path.exists()
        content = rc_path.read_text()
        assert "src/api" in content
        assert "/var/task" in content


class TestPytestConfigure:
    def test_registers_no_cov_container_option(self, pytester):
        result = pytester.runpytest("--help")
        result.stdout.fnmatch_lines(["*--no-cov-container*"])


class TestEndToEndDefaultPath:
    """Exercise the full move-and-shim chain: inject() then `bash run.sh`.

    Validates that the rendered wrapper runs the user's _orig_run.sh, that
    coverage data is written, and that exit code propagates from the child.
    No Docker dependency.
    """

    def test_end_to_end_default_path_writes_coverage(self, tmp_path):
        build = tmp_path / "build"
        build.mkdir()

        # User's tiny app
        app = build / "app.py"
        app.write_text(
            "def main():\n    return 1 + 2\n\nif __name__ == '__main__':\n    main()\n"
        )

        # User's run.sh exec's their app
        run_sh = build / "run.sh"
        run_sh.write_text(f"#!/bin/bash\nexec {sys.executable} {app}\n")
        run_sh.chmod(0o755)

        # site-packages with a `.pth` calling coverage.process_startup() —
        # the gate inject() enforces, AND the mechanism subprocess attach uses.
        sp = build / "site-packages"
        sp.mkdir()
        (sp / "coverage_subprocess.pth").write_text(
            "import coverage; coverage.process_startup()\n"
        )

        driver = PythonDriver()
        result = driver.inject(
            build,
            DriverConfig(build_dir=str(build), entrypoint=None, path_mapping={}),
        )
        assert len(result.files_written) == 4

        # Wrapper artifacts are hardcoded to /var/task; substitute to tmp build_dir.
        # (Production assumption: Lambda mounts build_dir at /var/task.)
        for name in (".coveragerc", "_cov_wrapper.py", "run.sh"):
            p = build / name
            p.write_text(p.read_text().replace("/var/task", str(build)))
        # Redirect the hardcoded /tmp data_file path so the test doesn't
        # pollute /tmp and is independent of parallel test runs.
        rc = build / ".coveragerc"
        rc.write_text(
            rc.read_text().replace(
                "/tmp/.coverage.container", str(build / ".coverage.container")
            )
        )
        # Re-set executable bit (write_text may have preserved it, but be safe)
        run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Execute the shim. coverage data file inside build_dir for isolation.
        # Inherit parent env (so `python` resolves to the venv interpreter w/
        # coverage installed) plus point COVERAGE_PROCESS_START at our rc and
        # PYTHONPATH at the site-packages whose .pth attaches subprocess cov.
        import os

        env = {
            **os.environ,
            "COVERAGE_PROCESS_START": str(build / ".coveragerc"),
            "PYTHONPATH": str(sp),
        }
        proc = subprocess.run(  # noqa: S603
            ["bash", str(run_sh)],
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert proc.returncode == 0, (
            f"wrapper exit nonzero: rc={proc.returncode}\n"
            f"stdout={proc.stdout.decode()}\nstderr={proc.stderr.decode()}"
        )

        # At least one .coverage.container* file written
        cov_files = list(build.glob(".coverage.container*"))
        assert cov_files, (
            f"no coverage data files in {build}; ls: {list(build.iterdir())}"
        )
        # Non-trivial size
        assert any(f.stat().st_size > 0 for f in cov_files)
