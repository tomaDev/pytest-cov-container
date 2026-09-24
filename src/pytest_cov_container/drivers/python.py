import json
import logging
import os
import shutil
import stat
import sys
import warnings
from pathlib import Path

from pytest_cov_container import protocol
from pytest_cov_container.models import (
    ContainerInfo,
    DockerBackendProtocol,
    DriverConfig,
    InjectionResult,
)

logger = logging.getLogger(__name__)

# Directory names never measured when ``source_dir`` enumerates the include list:
# test trees and caches are not deployed, and dot-dirs (``.venv``, ``.aws-sam``)
# hold third-party or build copies.
_SOURCE_SKIP_DIRS = frozenset({"__pycache__", "tests"})


def _include_patterns(config: DriverConfig, rootpath: Path | None) -> list[str]:
    """``include`` entries for the container rcfile.

    With ``source_dir``: every ``*.py`` under it, rendered at its container path,
    so vendored dependencies sharing the container root stay unmeasured.
    Without: ``*.py`` (everything the container imports).
    """
    if config.source_dir is None:
        return ["*.py"]
    source = Path(config.source_dir)
    if not source.is_absolute():
        if rootpath is None:
            raise ValueError("source_dir is relative but no rootpath was given")
        source = rootpath / source
    if not source.is_dir():
        raise FileNotFoundError(f"source_dir {source} does not exist")
    root = config.container_root.rstrip("/")
    patterns = []
    for path in sorted(source.rglob("*.py")):
        rel = path.relative_to(source)
        if any(part in _SOURCE_SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            continue
        patterns.append(f"{root}/{rel.as_posix()}")
    return patterns


def render_coveragerc(config: DriverConfig, *, rootpath: Path | None = None, branch: bool = False) -> str:
    """The ``.coveragerc`` the container-side coverage process reads.

    ``branch`` must equal the host's setting (``DriverConfig.branch`` overrides
    it); a statement-only container dataset cannot combine with branch data.
    """
    branch = branch if config.branch is None else config.branch
    include = "\n    ".join(_include_patterns(config, rootpath))
    return (
        "[run]\n"
        f"data_file = {protocol.DATA_DIR}/{protocol.DATA_PREFIX}\n"
        f"relative_files = {str(config.relative_files).lower()}\n"
        f"branch = {str(branch).lower()}\n"
        "parallel = true\n"
        "sigterm = true\n"
        "include =\n"
        f"    {include}\n"
        "omit =\n"
        "    */_cov_wrapper.py\n"
    )


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so a concurrent reader sees old or new, never partial.

    A container started by a parallel session can read the rcfile while this
    process rewrites it.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    tmp.replace(path)

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
# Save protocol (pytest_cov_container.protocol): the host signals the pid in
# this file, then waits for the sentinel written after each save.
with open(__PID_FILE__, "w") as _pid_f:
    _pid_f.write(str(os.getpid()))

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
    # Sentinel: the host copies data files only after this exists, so it
    # never reads a half-written SQLite file.
    open(__DONE_FILE__, "w").close()


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
with open(__PID_FILE__, "w") as _pid_f:
    _pid_f.write(str(os.getpid()))

proc: subprocess.Popen | None = None


def _save(_signum, _frame):
    cov.save()
    open(__DONE_FILE__, "w").close()


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
    #    of versions Lambda currently supports. dict.fromkeys preserves
    #    order and dedups the host version if it's already in the fallback list.
    host_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    candidates = list(
        dict.fromkeys(
            [host_ver, *(f"python3.{v}" for v in (15, 14, 13, 12, 11, 10, 9))]
        )
    )
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


def _render_wrapper(
    template: str,
    *,
    pid_file: str = protocol.PID_FILE,
    done_file: str = protocol.DONE_FILE,
) -> str:
    """Fill the protocol paths into a wrapper template.

    The paths are the protocol constants; tests pass host temp paths.
    """
    return template.replace("__PID_FILE__", repr(pid_file)).replace(
        "__DONE_FILE__", repr(done_file)
    )


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _coverage_env(config: DriverConfig) -> dict[str, str]:
    return {"COVERAGE_PROCESS_START": f"{config.container_root.rstrip('/')}/.coveragerc"}


def _inject_shim(target_dir: Path, config: DriverConfig, rcfile: str) -> InjectionResult:
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
    _atomic_write(coveragerc, rcfile)
    files.append(coveragerc)

    wrapper = target_dir / "_cov_wrapper.py"
    _atomic_write(wrapper, _render_wrapper(_COV_WRAPPER_TEMPLATE_SHIM))
    files.append(wrapper)

    _atomic_write(run_sh, _RUN_SH_TEMPLATE)
    _make_executable(run_sh)
    files.append(run_sh)

    _make_executable(orig)
    files.append(orig)

    return InjectionResult(files_written=files, env_vars=_coverage_env(config))


def _inject_legacy(target_dir: Path, config: DriverConfig, rcfile: str) -> InjectionResult:
    orig = target_dir / "_orig_run.sh"
    if orig.exists():
        logger.debug("override path: unlinking stale _orig_run.sh at %s", orig)
        orig.unlink()

    files: list[Path] = []

    coveragerc = target_dir / ".coveragerc"
    _atomic_write(coveragerc, rcfile)
    files.append(coveragerc)

    # Entrypoint is written to a sidecar JSON file and loaded by the wrapper
    # at runtime, rather than interpolated into the wrapper's Python source.
    # Prevents code-injection attacks via a hostile `entrypoint` value in
    # pyproject.toml (CWE-94 + CWE-78).
    entrypoint_json = target_dir / "_cov_entrypoint.json"
    _atomic_write(entrypoint_json, json.dumps({"entrypoint": config.entrypoint}))
    files.append(entrypoint_json)

    wrapper = target_dir / "_cov_wrapper.py"
    _atomic_write(wrapper, _render_wrapper(_COV_WRAPPER_TEMPLATE_LEGACY))
    files.append(wrapper)

    run_sh = target_dir / "run.sh"
    _atomic_write(run_sh, _RUN_SH_TEMPLATE)
    _make_executable(run_sh)
    files.append(run_sh)

    return InjectionResult(files_written=files, env_vars=_coverage_env(config))


def _inject_rcfile_only(target_dir: Path, config: DriverConfig, rcfile: str) -> InjectionResult:
    """``wrapper = false``: the application starts coverage and runs the save protocol."""
    coveragerc = target_dir / ".coveragerc"
    _atomic_write(coveragerc, rcfile)
    return InjectionResult(files_written=[coveragerc], env_vars=_coverage_env(config))


class PythonDriver:
    name: str = "python"

    def container_env(self, config: DriverConfig) -> dict[str, str]:
        """Env the container needs for coverage to start (see ``InjectionResult.env_vars``)."""
        return _coverage_env(config)

    def inject(
        self,
        target_dir: Path,
        config: DriverConfig,
        *,
        rootpath: Path | None = None,
        branch: bool = False,
    ) -> InjectionResult:
        rcfile = render_coveragerc(config, rootpath=rootpath, branch=branch)
        if not config.wrapper:
            return _inject_rcfile_only(target_dir, config, rcfile)
        if config.entrypoint is None:
            return _inject_shim(target_dir, config, rcfile)
        return _inject_legacy(target_dir, config, rcfile)

    def collect(
        self,
        docker_backend: DockerBackendProtocol,
        container: ContainerInfo,
        dest: Path,
        config: DriverConfig,  # noqa: ARG002
    ) -> list[Path]:
        """Save (running containers only), then copy the data files to ``dest``.

        A stopped container is read as-is: its process saved on exit, if at all.
        A running container without a coverage process (no pid file) is not an
        error here; the caller decides whether an empty result is.
        """
        if container.status == "running" and docker_backend.send_signal(container.id) > 0:
            if not docker_backend.wait_for_done(container.id):
                warnings.warn(
                    f"Container {container.name} ({container.id[:12]}): save "
                    f"sentinel {protocol.DONE_FILE} not written in time; the "
                    "copied data may be incomplete.",
                    UserWarning,
                    stacklevel=2,
                )
        return docker_backend.extract_matching_files(
            container.id, protocol.DATA_DIR, protocol.DATA_PREFIX, dest
        )
