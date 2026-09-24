"""Which containers belong to THIS test process.

Image pattern and Docker label say what a container is, not whose it is. Two
checkouts of the same project, or two pytest-xdist workers of one session, run
identical images with identical labels (``sam local`` sets only its own). Collecting
from a sibling's container merges foreign (often partial) coverage into this
run, and a reaper built on the same match kills the sibling's containers.

Two ownership keys narrow the match:

* ``mount_prefix`` — the container bind-mounts a directory under
  ``<rootdir>/<mount_prefix>/`` (default: the SAM build root, the parent of
  ``build_dir``). ``sam local`` mounts each function's build dir, so the mount
  source tells checkouts apart. The trailing separator matters: a
  nested checkout (``<rootdir>/.claude/worktrees/x/...``) or a sibling whose
  path merely string-prefixes ours (``<rootdir>-other/...``) is not ours.
* ``worker_env`` — the container's environment carries ``<worker_env>=<id>``,
  where ``<id>`` is this process's xdist worker id (``main`` without xdist).
  The test harness passes :func:`pytest_cov_container.container_env` into the
  container. Every worker mounts the same build dir, so the mount alone cannot
  tell workers apart.
"""

import fnmatch
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pytest_cov_container.config import PluginConfig
    from pytest_cov_container.docker_backend import DockerBackend
    from pytest_cov_container.models import ContainerInfo

MAIN_WORKER = "main"


def worker_id() -> str:
    """This process's xdist worker id (``gw0``, ``gw1``, …), else ``main``."""
    return os.environ.get("PYTEST_XDIST_WORKER", MAIN_WORKER)


def image_matches(refs: Iterable[str], patterns: Iterable[str]) -> bool:
    """True iff any image reference matches any fnmatch pattern."""
    refs = list(refs)
    return any(fnmatch.fnmatch(ref, pattern) for pattern in patterns for ref in refs)


def mounts_under(attrs: Mapping[str, Any], prefix: Path) -> bool:
    """True iff a bind-mount source of the container sits under ``prefix``.

    Both the literal and the resolved form of ``prefix`` are accepted, since
    Docker reports whichever path the creating tool passed.
    """
    roots = tuple({f"{prefix.as_posix()}/", f"{prefix.resolve().as_posix()}/"})
    sources = (str(mount.get("Source", "")) for mount in attrs.get("Mounts") or [])
    return any(source.startswith(roots) for source in sources)


def has_env(attrs: Mapping[str, Any], key: str, value: str) -> bool:
    """True iff the container env holds exactly ``key=value``.

    Exact match on purpose: worker ``gw1`` must not claim ``gw10``.
    """
    env = (attrs.get("Config") or {}).get("Env") or []
    return f"{key}={value}" in env


def find_owned(
    backend: "DockerBackend",
    cfg: "PluginConfig",
    rootpath: Path,
    *,
    all_workers: bool = False,
) -> "list[ContainerInfo]":
    """Containers matching ``cfg``'s image/label filters and ownership keys.

    ``all_workers`` drops the worker check (the mount check stays): a pre-flight
    sweep of a previous run's leftovers wants every worker's containers from
    this checkout, whatever worker id they carried.
    """
    wid = worker_id()

    def owns(attrs: Mapping[str, Any]) -> bool:
        if cfg.mount_prefix and not mounts_under(attrs, rootpath / cfg.mount_prefix):
            return False
        return all_workers or not cfg.worker_env or has_env(attrs, cfg.worker_env, wid)

    return backend.find_containers(
        image_pattern=cfg.image_patterns or None, label=cfg.label, predicate=owns
    )
