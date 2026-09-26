"""Drive the SAM project the way a consumer's integration tests would."""

import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import pytest_cov_container

# The first call of a runtime builds SAM's image for it.
_SAM_TIMEOUT_S = 300
# Each call otherwise asks the registry whether the runtime image is current
# (about 1.6s). An image that is not local yet is still pulled.
_SKIP_PULL = ("--skip-pull-image",)
_POLL_S = 0.25


def _env_file(root: Path, tmp_path: Path) -> Path:
    path = tmp_path / "env.json"
    path.write_text(json.dumps({"Parameters": pytest_cov_container.container_env(root)}))
    return path


@pytest.fixture
def invoke(request, tmp_path):
    """``sam local invoke <function>`` with ``event``; returns the parsed response."""
    root = request.config.rootpath
    env_file = _env_file(root, tmp_path)

    def run(function: str, event: dict) -> dict:
        event_file = tmp_path / f"{function}-event.json"
        event_file.write_text(json.dumps(event))
        proc = subprocess.run(
            [
                "sam", "local", "invoke", function,
                "--event", str(event_file),
                "--env-vars", str(env_file),
                *_SKIP_PULL,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=_SAM_TIMEOUT_S,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        response = json.loads(proc.stdout)
        assert "errorType" not in response, f"{response}\n{proc.stderr}"
        return response

    return run


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=60) as response:  # noqa: S310 — local sam endpoint
        return json.load(response)


@pytest.fixture
def api(request, tmp_path):
    """``sam local start-api`` with warm containers; yields ``get(path) -> body``."""
    root = request.config.rootpath
    port = _free_port()
    log = (tmp_path / "start-api.log").open("w")
    proc = subprocess.Popen(
        [
            "sam", "local", "start-api",
            "--port", str(port),
            "--warm-containers", "EAGER",
            "--env-vars", str(_env_file(root, tmp_path)),
            *_SKIP_PULL,
        ],
        cwd=root,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + _SAM_TIMEOUT_S
        while True:
            assert proc.poll() is None, (tmp_path / "start-api.log").read_text()
            try:
                _get(f"{url}/node")
                break
            except (urllib.error.URLError, ConnectionError):
                assert time.monotonic() < deadline, (tmp_path / "start-api.log").read_text()
                time.sleep(_POLL_S)
        yield lambda path: _get(f"{url}{path}")
    finally:
        proc.terminate()
        proc.wait(timeout=60)
        log.close()
