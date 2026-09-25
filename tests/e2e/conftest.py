"""End-to-end: the installed plugin against real ``sam local`` containers.

Each test runs pytest with ``--cov`` in a fresh copy of ``sam_app`` (a small
SAM project whose tests invoke its functions), then reads the combined
coverage data. Run with ``hatch test -m e2e``; skipped without Docker or SAM CLI.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).with_name("sam_app")
# The project's own tests run only inside the e2e tests' pytest runs.
collect_ignore = ["sam_app"]

_BUILD_TIMEOUT_S = 600


def _available(*cmd: str) -> bool:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=60, check=False).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.fixture(scope="session")
def built_app(tmp_path_factory) -> Path:
    """``sam_app`` copied and built once per session."""
    if not _available("docker", "info"):
        pytest.skip("Docker engine not reachable")
    if not _available("sam", "--version"):
        pytest.skip("SAM CLI not installed")
    root = tmp_path_factory.mktemp("sam_app")
    shutil.copytree(APP, root, dirs_exist_ok=True, ignore=shutil.ignore_patterns(".aws-sam", "__pycache__"))
    build = subprocess.run(
        ["sam", "build"], cwd=root, capture_output=True, text=True, timeout=_BUILD_TIMEOUT_S, check=False
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return root


@pytest.fixture
def app(built_app, tmp_path) -> Path:
    """A copy of the built project per test: the plugin injects into its build dirs."""
    root = tmp_path / "app"
    shutil.copytree(built_app, root)
    return root
