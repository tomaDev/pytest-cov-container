import stat

from pytest_cov_container.drivers.python import PythonDriver
from pytest_cov_container.models import DriverConfig, InjectionResult


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
