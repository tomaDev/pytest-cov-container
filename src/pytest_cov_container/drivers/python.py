import logging
import shutil
import stat
import sys
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

# Self-locate: build_dir is wherever this wrapper sits. On Lambda that's
# /var/task; on non-SAM deployments it may be anywhere. Computing the
# path at runtime removes the Lambda assumption and makes the wrapper
# portable (and trivially testable).
_HERE = os.path.dirname(os.path.abspath(__file__))

# Propagate COVERAGE_PROCESS_START to child subprocess Python interpreters so
# subprocess coverage activates via coverage.process_startup() in each child.
# setdefault, not assignment: respect any caller-supplied override.
os.environ.setdefault("COVERAGE_PROCESS_START", os.path.join(_HERE, ".coveragerc"))

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

proc = subprocess.Popen(["bash", os.path.join(_HERE, "_orig_run.sh")])
rc = proc.wait()
cov.stop()
cov.save()
raise SystemExit(rc)
"""

_COV_WRAPPER_TEMPLATE_LEGACY = """\
import json
import os
import signal
import subprocess

import coverage

# Self-locate (see comment in shim template).
_HERE = os.path.dirname(os.path.abspath(__file__))

# Entrypoint is read from a sidecar JSON file at runtime, not interpolated
# into this source. This eliminates a two-layer injection (CWE-94 + CWE-78)
# that previously existed when `_inject_legacy` did `template.format(
# entrypoint=cfg.entrypoint)` and a hostile pyproject.toml could plant
# Python source in the wrapper.
with open(os.path.join(_HERE, "_cov_entrypoint.json")) as _f:
    _entrypoint_data = json.load(_f)
_entrypoint = _entrypoint_data["entrypoint"]

cov = coverage.Coverage(
    config_file=os.environ.get("COVERAGE_PROCESS_START", os.path.join(_HERE, ".coveragerc"))
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
    ["sh", "-c", os.environ.get("CONTAINER_COV_ENTRYPOINT", _entrypoint)]
)
rc = proc.wait()
cov.stop()
cov.save()
raise SystemExit(rc)
"""

_RUN_SH_TEMPLATE = """\
#!/bin/bash
# Self-locating shim: invoke the wrapper next to this script, whatever
# directory the container has mounted us at. Removes the Lambda-only
# `/var/task` assumption.
exec python "$(cd "$(dirname "$0")" && pwd)/_cov_wrapper.py"
"""


def _is_shim(run_sh: Path) -> bool:
    """Heuristic shim detection: run.sh references _cov_wrapper.py."""
    try:
        return "_cov_wrapper.py" in run_sh.read_text()
    except (OSError, UnicodeDecodeError):
        return False


def _find_coverage_pth(build_dir: Path) -> Path | None:
    """Locate coverage's subprocess `.pth` file in a SAM-style build_dir.

    Direct probes against the conventional layouts — no recursive scan, since
    a real SAM build_dir routinely holds 60k-120k files and a warm-cache
    recursive walk runs 200-800ms. Three probe shapes cover every layout
    we've seen in production:

    1. ``<build_dir>/coverage*.pth``               — flat function build
       (`sam build` Python function; pip installs deps at the build root).
    2. ``<build_dir>/python{X.Y}/site-packages/`` — Lambda layer style.
    3. ``<build_dir>/*/site-packages/``           — one-level fallback for
       unusual layouts (containers, vendored bundles, etc.).
    """
    # 1. Flat function build (sam build Python function planted at root).
    direct = list(build_dir.glob("coverage*.pth"))
    if direct:
        return direct[0]
    # 1b. Direct site-packages subdir under build_dir.
    direct_sp = build_dir / "site-packages"
    if direct_sp.is_dir():
        hits = list(direct_sp.glob("coverage*.pth"))
        if hits:
            return hits[0]
    # 2. Versioned site-packages. Try the host's Python first (most likely
    #    match for a `sam build` run on the same host), then the small set
    #    of versions Lambda currently supports.
    candidates = [f"python{sys.version_info.major}.{sys.version_info.minor}"]
    candidates += [
        f"python3.{v}"
        for v in (15, 14, 13, 12, 11, 10, 9)
        if f"python3.{v}" not in candidates
    ]
    for pyver in candidates:
        sp = build_dir / pyver / "site-packages"
        if sp.is_dir():
            hits = list(sp.glob("coverage*.pth"))
            if hits:
                return hits[0]
    # 3. One-level fallback for non-versioned layouts (e.g. `python/`).
    for sp in build_dir.glob("*/site-packages"):
        hits = list(sp.glob("coverage*.pth"))
        if hits:
            return hits[0]
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
    import json

    orig = target_dir / "_orig_run.sh"
    if orig.exists():
        logger.debug("override path: unlinking stale _orig_run.sh at %s", orig)
        orig.unlink()

    files: list[Path] = []

    coveragerc = target_dir / ".coveragerc"
    coveragerc.write_text(_COVERAGERC_TEMPLATE)
    files.append(coveragerc)

    # Entrypoint is written to a sidecar JSON file and loaded by the wrapper
    # at runtime, rather than interpolated into the wrapper's Python source.
    # Prevents code-injection attacks via a hostile `entrypoint` value in
    # pyproject.toml (CWE-94 + CWE-78).
    entrypoint_json = target_dir / "_cov_entrypoint.json"
    entrypoint_json.write_text(json.dumps({"entrypoint": config.entrypoint}))
    files.append(entrypoint_json)

    wrapper = target_dir / "_cov_wrapper.py"
    wrapper.write_text(_COV_WRAPPER_TEMPLATE_LEGACY)
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
            # Capture pre-signal file signature, send SIGUSR1, then poll for
            # the wrapper's cov.save() to land (replaces the legacy fixed
            # 1-second sleep that both wasted ~1s on fast hosts and racing
            # on slow ones).
            baseline = docker_backend.file_signature(
                container.id,
                "/tmp",  # noqa: S108
                ".coverage.container",
            )
            docker_backend.send_signal(container.id)
            docker_backend.wait_for_save(
                container.id,
                "/tmp",  # noqa: S108
                ".coverage.container",
                baseline,
            )

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
