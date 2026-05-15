import logging
import shutil
import stat
import time
import warnings
from pathlib import Path

from pytest_cov_container.models import (
    ContainerInfo,
    DockerBackendProtocol,
    DriverConfig,
    InjectionResult,
)

logger = logging.getLogger(__name__)

_COVERAGERC_TEMPLATE = """\
[run]
data_file = /tmp/.coverage.container
relative_files = true
parallel = true
sigterm = true
include =
    *.py
omit =
    _cov_wrapper.py
"""

_COV_WRAPPER_TEMPLATE_SHIM = """\
import os
import signal
import subprocess

import coverage

# Propagate COVERAGE_PROCESS_START to child subprocess Python interpreters so
# subprocess coverage activates via coverage.process_startup() in each child.
# setdefault, not assignment: respect any caller-supplied override.
os.environ.setdefault("COVERAGE_PROCESS_START", "/var/task/.coveragerc")

cov = coverage.Coverage(config_file=os.environ["COVERAGE_PROCESS_START"])
cov.start()

# `proc` must exist as a name before signal handlers are installed because
# both handlers close over it. Pre-declare to None to make the closure safe
# even if a signal arrives in the window between handler install and Popen
# assignment.
proc: subprocess.Popen | None = None


def _save(_signum, _frame):
    # SIGUSR1: host-initiated collect. Save wrapper coverage only.
    # Do NOT forward — default Linux disposition for SIGUSR1 is `terminate`
    # and the user app rarely installs a handler; forwarding would kill it.
    cov.save()


def _save_and_forward(signum, _frame):
    # SIGTERM: container shutdown. Save wrapper, then forward to child so
    # `proc.wait()` returns instead of hanging until SIGKILL.
    # Installed AFTER cov.start(): replaces coverage's `sigterm = true`
    # handler on the wrapper PID. Children attach independently via the
    # `coverage*.pth` in site-packages and keep `sigterm = true` semantics.
    cov.save()
    if proc is not None and proc.poll() is None:
        proc.send_signal(signum)


# Install handlers BEFORE Popen. Closes the race window where a signal during
# process spawn would fall through to Python's defaults.
signal.signal(signal.SIGUSR1, _save)
signal.signal(signal.SIGTERM, _save_and_forward)

proc = subprocess.Popen(["bash", "/var/task/_orig_run.sh"])
rc = proc.wait()
cov.stop()
cov.save()
raise SystemExit(rc)
"""

_COV_WRAPPER_TEMPLATE_LEGACY = """\
import os
import signal
import subprocess

import coverage

cov = coverage.Coverage(
    config_file=os.environ.get("COVERAGE_PROCESS_START", "/var/task/.coveragerc")
)
cov.start()

proc: subprocess.Popen | None = None


def _save(_signum, _frame):
    cov.save()


def _save_and_forward(signum, _frame):
    cov.save()
    if proc is not None and proc.poll() is None:
        proc.send_signal(signum)


signal.signal(signal.SIGUSR1, _save)
signal.signal(signal.SIGTERM, _save_and_forward)

proc = subprocess.Popen(
    ["sh", "-c", os.environ.get("CONTAINER_COV_ENTRYPOINT", "{entrypoint}")]
)
rc = proc.wait()
cov.stop()
cov.save()
raise SystemExit(rc)
"""

_RUN_SH_TEMPLATE = """\
#!/bin/bash
exec python /var/task/_cov_wrapper.py
"""


def _is_shim(run_sh: Path) -> bool:
    """Heuristic shim detection: run.sh references _cov_wrapper.py."""
    try:
        return "_cov_wrapper.py" in run_sh.read_text()
    except (OSError, UnicodeDecodeError):
        return False


def _find_coverage_pth(build_dir: Path) -> Path | None:
    for sp in build_dir.rglob("site-packages"):
        if not sp.is_dir():
            continue
        matches = list(sp.glob("coverage*.pth"))
        if matches:
            return matches[0]
    return None


def _inject_shim(target_dir: Path, config: DriverConfig) -> InjectionResult:  # noqa: ARG001
    run_sh = target_dir / "run.sh"
    orig = target_dir / "_orig_run.sh"

    if not run_sh.exists():
        raise RuntimeError(
            f"build_dir at {target_dir} has no run.sh. Run `sam build` (or "
            "equivalent) before tests, or set "
            "[tool.pytest-cov-container.python].entrypoint to use the legacy "
            "override path."
        )
    # readability check (raises PermissionError as documented)
    with run_sh.open("rb"):
        pass

    if _find_coverage_pth(target_dir) is None:
        raise RuntimeError(
            f"build_dir at {target_dir} has no installed `coverage` package; "
            "subprocess coverage cannot attach. Add `coverage` to the "
            "application's dependencies and re-run `sam build`."
        )

    if _is_shim(run_sh) and not orig.exists():
        raise RuntimeError(
            f"build_dir at {target_dir} contains a shim `run.sh` but no "
            "`_orig_run.sh` to recover from; run `sam build` to reset."
        )

    if orig.exists() and not _is_shim(run_sh):
        warnings.warn(
            f"build_dir at {target_dir} has `_orig_run.sh` but `run.sh` is "
            "not the injected shim; rewriting both.",
            UserWarning,
            stacklevel=2,
        )
        # Re-snapshot the current run.sh as the new _orig_run.sh so the user's
        # most recent intent wins.
        shutil.copy2(run_sh, orig)
    elif not orig.exists():
        shutil.copy2(run_sh, orig)

    files: list[Path] = []

    coveragerc = target_dir / ".coveragerc"
    coveragerc.write_text(_COVERAGERC_TEMPLATE)
    files.append(coveragerc)

    wrapper = target_dir / "_cov_wrapper.py"
    wrapper.write_text(_COV_WRAPPER_TEMPLATE_SHIM)
    files.append(wrapper)

    run_sh.write_text(_RUN_SH_TEMPLATE)
    run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    files.append(run_sh)

    orig.chmod(orig.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    files.append(orig)

    return InjectionResult(
        files_written=files,
        env_vars={"COVERAGE_PROCESS_START": "/var/task/.coveragerc"},
    )


def _inject_legacy(target_dir: Path, config: DriverConfig) -> InjectionResult:
    orig = target_dir / "_orig_run.sh"
    if orig.exists():
        logger.debug("override path: unlinking stale _orig_run.sh at %s", orig)
        orig.unlink()

    files: list[Path] = []

    coveragerc = target_dir / ".coveragerc"
    coveragerc.write_text(_COVERAGERC_TEMPLATE)
    files.append(coveragerc)

    wrapper = target_dir / "_cov_wrapper.py"
    wrapper.write_text(
        _COV_WRAPPER_TEMPLATE_LEGACY.format(entrypoint=config.entrypoint)
    )
    files.append(wrapper)

    run_sh = target_dir / "run.sh"
    run_sh.write_text(_RUN_SH_TEMPLATE)
    run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    files.append(run_sh)

    return InjectionResult(
        files_written=files,
        env_vars={"COVERAGE_PROCESS_START": "/var/task/.coveragerc"},
    )


class PythonDriver:
    name: str = "python"

    def inject(self, target_dir: Path, config: DriverConfig) -> InjectionResult:
        if config.entrypoint is None:
            return _inject_shim(target_dir, config)
        return _inject_legacy(target_dir, config)

    def collect(
        self,
        docker_backend: DockerBackendProtocol,
        container: ContainerInfo,
        dest: Path,
        config: DriverConfig,  # noqa: ARG002
    ) -> Path:
        if container.status == "running":
            docker_backend.send_signal(container.id)
            time.sleep(1)

        extracted = docker_backend.extract_matching_files(
            container.id,
            "/tmp",  # noqa: S108
            ".coverage.container",
            dest,
        )

        if not extracted:
            warnings.warn(
                f"No coverage data found in container {container.name} ({container.id[:12]})",
                UserWarning,
                stacklevel=2,
            )

        return dest
