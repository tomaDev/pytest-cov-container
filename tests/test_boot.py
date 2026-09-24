"""The container bootstrap, run for real on the host the way the Lambda runtime runs it."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from coverage.data import CoverageData

from pytest_cov_container import _boot, inject, protocol
from pytest_cov_container.frameworks import FunctionTarget, SourceMapping
from pytest_cov_container.sink import CoverageSink

HANDLER = """\
def helper(n):
    if n > 1:
        return "big"
    return "small"


def handler(event, context):
    return helper(event["n"])
"""

# What /var/runtime/bootstrap.py does before it imports the handler.
RUNTIME = """\
import site, sys
task_root = sys.argv[1]
sys.path.insert(0, task_root)
site.addsitedir(task_root)
import importlib
module = importlib.import_module(sys.argv[2])
print(module.handler({"n": 5}, None))
"""

ASYNC_HANDLER = """\
import asyncio


async def handler(event, context):
    await asyncio.sleep(0)
    if event["n"] > 1:
        return "big"
    return "small"
"""

# A runtime that awaits an ``async def`` handler.
ASYNC_RUNTIME = """\
import asyncio, site, sys
task_root = sys.argv[1]
sys.path.insert(0, task_root)
site.addsitedir(task_root)
import importlib
module = importlib.import_module(sys.argv[2])
print(asyncio.run(module.handler({"n": 5}, None)))
"""

SERVER = """\
import site, sys, time
site.addsitedir(sys.argv[1])
print("ready", flush=True)
time.sleep(30)
"""


def _build(tmp_path: Path, handler: str) -> Path:
    """A built function whose task root is a host dir, injected by the plugin."""
    source = tmp_path / "src" / "fn"
    source.mkdir(parents=True)
    (source / "handler.py").write_text(handler)
    build = tmp_path / "build" / "Fn"
    build.mkdir(parents=True)
    (build / "handler.py").write_text(handler)
    target = FunctionTarget("Fn", str(build), str(build), (SourceMapping(str(source), str(build)),))
    inject.inject(target, rootpath=tmp_path, branch=True)
    return build


@pytest.fixture
def task(tmp_path):
    return _build(tmp_path, HANDLER)


@pytest.fixture
def async_task(tmp_path):
    return _build(tmp_path, ASYNC_HANDLER)


@pytest.fixture
def sink():
    pushes: list[tuple[str, str, bytes]] = []
    server = CoverageSink(bind="127.0.0.1", advertise_host="127.0.0.1", on_push=lambda *push: pushes.append(push))
    server.pushes = pushes
    server.start()
    yield server
    server.stop()


def _env(task: Path, sink: CoverageSink, tmp_path: Path, **extra) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COV_", "COVERAGE_"))}
    env |= {
        protocol.RCFILE_ENV: str(task / protocol.RCFILE),
        protocol.SINK_ENV: sink.url,
        _boot.PID_FILE_ENV: str(tmp_path / "pid"),
        "_HANDLER": "handler.handler",
    }
    return env | extra


def _lines(body: bytes, tmp_path: Path) -> dict[str, list[int]]:
    path = tmp_path / "pushed"
    path.write_bytes(body)
    data = CoverageData(basename=str(path))
    data.read()
    return {Path(f).name: sorted(data.lines(f) or []) for f in data.measured_files()}


def test_pushes_after_each_handler_call(task, sink, tmp_path):
    out = subprocess.run(
        [sys.executable, "-c", RUNTIME, str(task), "handler"],
        check=False, env=_env(task, sink, tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "big"
    (ident, function, body), *_ = sink.pushes
    assert function == "Fn"
    assert ident.endswith(f"-{(tmp_path / 'pid').read_text()}")
    # The handler module was measured from its first line: coverage started
    # before the runtime imported it.
    assert _lines(body, tmp_path) == {"handler.py": [1, 2, 3, 7, 8]}


def test_an_async_handler_pushes_after_its_body_ran(async_task, sink, tmp_path):
    # Calling an async handler only creates the coroutine; a push at that
    # point would miss the body. The atexit push comes later and has it all,
    # so only the first push tells.
    out = subprocess.run(
        [sys.executable, "-c", ASYNC_RUNTIME, str(async_task), "handler"],
        check=False,
        env=_env(async_task, sink, tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "big"
    (_, _, body), *_ = sink.pushes
    assert _lines(body, tmp_path) == {"handler.py": [1, 4, 5, 6, 7]}


def test_inert_without_the_sink(task, sink, tmp_path):
    env = _env(task, sink, tmp_path)
    del env[protocol.SINK_ENV]
    out = subprocess.run(
        [sys.executable, "-c", RUNTIME, str(task), "handler"], check=False, env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, out.stderr
    assert sink.pushes == []
    assert not (tmp_path / "pid").exists()


def test_a_failed_push_never_breaks_the_function(task, sink, tmp_path):
    env = _env(task, sink, tmp_path, **{protocol.SINK_ENV: "http://127.0.0.1:9/nowhere"})
    out = subprocess.run(
        [sys.executable, "-c", RUNTIME, str(task), "handler"], check=False, env=env, capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0
    assert out.stdout.strip() == "big"
    assert "coverage push failed" in out.stderr


def test_sigusr1_pushes_a_long_running_process(task, sink, tmp_path):
    proc = subprocess.Popen(
        [sys.executable, "-c", SERVER, str(task)],
        env=_env(task, sink, tmp_path, _HANDLER="run.sh"),
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        os.kill(int((tmp_path / "pid").read_text()), signal.SIGUSR1)
        deadline = time.monotonic() + 10
        while not sink.pushes and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sink.pushes
        assert sink.pushes[0][1] == "Fn"
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize(
    ("handler", "expected"),
    [
        ("handler.handler", ("handler", "handler")),
        ("pkg/mod.fn", ("pkg.mod", "fn")),
        ("run.sh", None),
        ("", None),
        ("nodot", None),
    ],
)
def test_handler_target(handler, expected):
    assert _boot._handler_target(handler) == expected
