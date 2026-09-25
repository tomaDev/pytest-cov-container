import os
import subprocess
import sys
from pathlib import Path

import pytest
from coverage.data import CoverageData

pytestmark = pytest.mark.e2e

SYNC = "src/sync/app.py"
LAYER = "src/layers/shared/shared/util.py"
MADE_LAYER = "src/layers/made/src/made/greet.py"
_RUN_TIMEOUT_S = 900


def run_pytest(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """pytest with ``--cov`` in ``root``, isolated from this session's own pytest and coverage."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COV_", "COVERAGE_"))}
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--cov=src", "--cov-report=", "-p", "no:randomly", *args],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_S,
        check=False,
    )


def covered_lines(root: Path) -> dict[str, set[int]]:
    """Lines in the combined data file, by path relative to ``root``."""
    data = CoverageData(basename=str(root / ".coverage"))
    data.read()
    base = root.resolve()
    lines = {}
    for measured in data.measured_files():
        path = Path(measured).resolve()
        key = path.relative_to(base).as_posix() if path.is_relative_to(base) else measured
        lines[key] = set(data.lines(measured) or ())
    return lines


def _passed(result) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def test_invoke_pushes_after_each_call(app):
    """`sam local invoke` removes its container right after the call."""
    _passed(run_pytest(app, "tests/test_invoke.py"))
    lines = covered_lines(app)
    assert {8, 9, 10, 12, 13} <= lines[SYNC]
    assert {2} <= lines[LAYER]  # layer code, built into /opt/python
    assert {2} <= lines[MADE_LAYER]  # built by a Makefile that moves src/ to python/


def test_flush_of_warm_start_api_containers(app):
    _passed(run_pytest(app, "tests/test_api.py"))
    lines = covered_lines(app)
    assert {8, 9, 10, 13} <= lines[SYNC]
    assert {2} <= lines[LAYER]
    assert {2} <= lines[MADE_LAYER]


def test_xdist_workers_each_receive_their_own_containers(app):
    # One invoke per worker: each branch arm is only covered when its
    # worker's sink received that worker's container push.
    _passed(run_pytest(app, "tests/test_invoke.py", "-n", "2"))
    assert {8, 9, 10, 12, 13} <= covered_lines(app)[SYNC]
