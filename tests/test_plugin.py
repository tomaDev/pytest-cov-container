from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pytest_cov_container.config import PluginConfig
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
    def test_injects_when_build_dir_exists(self, mock_backend_cls, plugin_config, tmp_path):  # noqa: ARG002
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
    def test_raises_when_build_dir_missing(self, mock_backend_cls, plugin_config, tmp_path):  # noqa: ARG002
        plugin = ContainerCovPlugin(plugin_config)
        mock_session = MagicMock()
        mock_session.config.rootpath = tmp_path

        with pytest.raises(FileNotFoundError, match="does not exist"):
            plugin.pytest_sessionstart(mock_session)


class TestContainerCovPluginSessionFinish:
    @patch("pytest_cov_container.plugin.DockerBackend")
    @patch("subprocess.run")
    def test_warns_when_no_containers_found(self, mock_subprocess, mock_backend_cls, plugin_config):  # noqa: ARG002
        plugin = ContainerCovPlugin(plugin_config)
        plugin.backend.find_containers.return_value = []

        mock_session = MagicMock()
        mock_session.config.rootpath = Path("/project")
        with pytest.warns(UserWarning, match="No matching containers"):
            plugin.pytest_sessionfinish(mock_session, exitstatus=0)

    @patch("pytest_cov_container.plugin.DockerBackend")
    @patch("subprocess.run")
    def test_collects_and_combines(self, mock_subprocess, mock_backend_cls, plugin_config):  # noqa: ARG002
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
        assert cmd[0] == "coverage"
        assert cmd[1] == "combine"

    @patch("pytest_cov_container.plugin.DockerBackend")
    @patch("subprocess.run")
    def test_writes_paths_config(self, mock_subprocess, mock_backend_cls, plugin_config):  # noqa: ARG002
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
