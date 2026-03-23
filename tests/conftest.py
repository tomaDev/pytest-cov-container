from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest_plugins = ["pytester"]


@pytest.fixture
def sample_pyproject(tmp_path):
    """Write a minimal pyproject.toml with plugin config and return its path."""
    content = """\
[tool.pytest-cov-container]
image_pattern = "samcli/lambda*"
label = "pytest-cov-container"

[tool.pytest-cov-container.path_mapping]
"src/api" = "/var/task"

[tool.pytest-cov-container.python]
build_dir = ".aws-sam/build/ApiFunction"
entrypoint = "uvicorn app:app --host 0.0.0.0 --port 8080"
"""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(content)
    return pyproject


@pytest.fixture
def mock_docker_container():
    """Return a MagicMock mimicking a docker Container object."""
    container = MagicMock()
    container.id = "abc123def456"
    container.name = "sam-local-api"
    container.image.tags = ["samcli/lambda-python:3.12"]
    container.labels = {"pytest-cov-container": "true"}
    container.status = "running"
    container.attrs = {"Config": {"Image": "samcli/lambda-python:3.12"}}
    return container


@pytest.fixture
def mock_docker_client(mock_docker_container):
    """Return a MagicMock mimicking docker.DockerClient."""
    client = MagicMock()
    client.containers.list.return_value = [mock_docker_container]
    client.containers.get.return_value = mock_docker_container
    return client
