import pytest

from pytest_cov_container import config


class TestLoadConfig:
    def test_returns_none_when_file_missing(self, tmp_path):
        result = config.load_config(tmp_path / "nonexistent.toml")
        assert result is None

    def test_returns_none_when_no_plugin_section(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'foo'\n")
        result = config.load_config(pyproject)
        assert result is None

    def test_loads_full_config(self, sample_pyproject):
        result = config.load_config(sample_pyproject)
        assert result is not None
        assert result.image_pattern == "samcli/lambda*"
        assert result.label == "pytest-cov-container"
        assert result.language == "python"
        assert result.enabled is True
        assert result.path_mapping == {"src/api": "/var/task"}

    def test_loads_driver_config(self, sample_pyproject):
        result = config.load_config(sample_pyproject)
        assert result.driver_config is not None
        assert result.driver_config.build_dir == ".aws-sam/build/ApiFunction"
        assert (
            result.driver_config.entrypoint
            == "uvicorn app:app --host 0.0.0.0 --port 8080"
        )
        assert result.driver_config.path_mapping == {"src/api": "/var/task"}

    def test_defaults_when_minimal_config(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[tool.pytest-cov-container]\nimage_pattern = "myapp*"\n')
        result = config.load_config(pyproject)
        assert result.language == "python"
        assert result.enabled is True
        assert result.path_mapping == {}
        assert result.driver_config.build_dir == ".aws-sam/build/ApiFunction"

    def test_disabled_config(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[tool.pytest-cov-container]\nenabled = false\n")
        result = config.load_config(pyproject)
        assert result.enabled is False

    def test_load_config_entrypoint_omitted_yields_none(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[tool.pytest-cov-container]\n"
            'image_pattern = "myapp*"\n'
            "[tool.pytest-cov-container.python]\n"
            'build_dir = ".aws-sam/build/ApiFunction"\n'
        )
        result = config.load_config(pyproject)
        assert result.driver_config.entrypoint is None

    def test_load_config_entrypoint_empty_raises(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[tool.pytest-cov-container]\n"
            'image_pattern = "myapp*"\n'
            "[tool.pytest-cov-container.python]\n"
            'entrypoint = ""\n'
        )
        with pytest.raises(ValueError, match="entrypoint is empty"):
            config.load_config(pyproject)

    def test_load_config_entrypoint_preserved_when_set(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            "[tool.pytest-cov-container]\n"
            'image_pattern = "myapp*"\n'
            "[tool.pytest-cov-container.python]\n"
            'entrypoint = "x y z"\n'
        )
        result = config.load_config(pyproject)
        assert result.driver_config.entrypoint == "x y z"
