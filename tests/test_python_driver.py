import stat
from unittest.mock import MagicMock

import pytest

from pytest_cov_container.drivers.python import PythonDriver
from pytest_cov_container.models import ContainerInfo, DriverConfig, InjectionResult


class TestPythonDriverInject:
    def setup_method(self):
        self.driver = PythonDriver()
        self.config = DriverConfig(
            build_dir=".aws-sam/build/ApiFunction",
            entrypoint="uvicorn app:app --host 0.0.0.0 --port 8080",
            path_mapping={"src/api": "/var/task"},
        )

    def test_creates_coveragerc(self, tmp_path):
        self.driver.inject(tmp_path, self.config)
        coveragerc = tmp_path / ".coveragerc"
        assert coveragerc.exists()
        content = coveragerc.read_text()
        assert "relative_files = true" in content
        assert "parallel = true" in content
        assert "sigterm = true" in content
        assert "data_file = /tmp/.coverage.container" in content
        assert "_cov_wrapper.py" in content  # in omit section

    def test_creates_cov_wrapper(self, tmp_path):
        self.driver.inject(tmp_path, self.config)
        wrapper = tmp_path / "_cov_wrapper.py"
        assert wrapper.exists()
        content = wrapper.read_text()
        assert "coverage.Coverage" in content
        assert "SIGUSR1" in content
        assert "CONTAINER_COV_ENTRYPOINT" in content
        assert self.config.entrypoint in content

    def test_creates_run_sh(self, tmp_path):
        self.driver.inject(tmp_path, self.config)
        run_sh = tmp_path / "run.sh"
        assert run_sh.exists()
        content = run_sh.read_text()
        assert "_cov_wrapper.py" in content
        # Verify executable
        assert run_sh.stat().st_mode & stat.S_IEXEC

    def test_returns_injection_result(self, tmp_path):
        result = self.driver.inject(tmp_path, self.config)
        assert isinstance(result, InjectionResult)
        assert len(result.files_written) == 3
        assert "COVERAGE_PROCESS_START" in result.env_vars

    def test_entrypoint_baked_into_wrapper(self, tmp_path):
        custom_config = DriverConfig(
            build_dir="build",
            entrypoint="gunicorn app:app -b 0.0.0.0:9000",
            path_mapping={},
        )
        self.driver.inject(tmp_path, custom_config)
        content = (tmp_path / "_cov_wrapper.py").read_text()
        assert "gunicorn app:app -b 0.0.0.0:9000" in content


class TestPythonDriverCollect:
    def setup_method(self):
        self.driver = PythonDriver()
        self.config = DriverConfig(
            build_dir=".aws-sam/build/ApiFunction",
            entrypoint="uvicorn app:app --host 0.0.0.0 --port 8080",
            path_mapping={"src/api": "/var/task"},
        )
        self.container_running = ContainerInfo(
            id="abc123",
            name="sam-api",
            image="samcli/lambda:3.12",
            labels={},
            status="running",
        )
        self.container_stopped = ContainerInfo(
            id="def456",
            name="sam-api",
            image="samcli/lambda:3.12",
            labels={},
            status="exited",
        )

    def test_signals_running_container(self, tmp_path):
        mock_backend = MagicMock()
        mock_backend.extract_matching_files.return_value = []
        self.driver.collect(mock_backend, self.container_running, tmp_path, self.config)
        mock_backend.send_signal.assert_called_once_with("abc123")

    def test_skips_signal_for_stopped_container(self, tmp_path):
        mock_backend = MagicMock()
        mock_backend.extract_matching_files.return_value = []
        self.driver.collect(mock_backend, self.container_stopped, tmp_path, self.config)
        mock_backend.send_signal.assert_not_called()

    def test_extracts_coverage_files(self, tmp_path):
        mock_backend = MagicMock()
        cov_file = tmp_path / ".coverage.container.host.1.abc"
        cov_file.write_bytes(b"data")
        mock_backend.extract_matching_files.return_value = [cov_file]

        result = self.driver.collect(mock_backend, self.container_stopped, tmp_path, self.config)

        mock_backend.extract_matching_files.assert_called_once_with("def456", "/tmp", ".coverage.container", tmp_path)
        assert result == tmp_path

    def test_warns_when_no_coverage_data(self, tmp_path):
        mock_backend = MagicMock()
        mock_backend.extract_matching_files.return_value = []

        with pytest.warns(UserWarning, match="No coverage data found"):
            self.driver.collect(mock_backend, self.container_stopped, tmp_path, self.config)
