# SPDX-FileCopyrightText: 2026-present tomaDev <genins21@gmail.com>
#
# SPDX-License-Identifier: MIT
"""Public API surface for pytest-cov-container.

User-facing helpers:

* :func:`collect_container_coverage` — save and collect the running containers
  this process owns; call it before stopping them.
* :func:`container_env` — the env vars a container needs: the coverage
  bootstrap (only while the plugin is active) plus the worker ownership marker.
* :func:`owned_containers` — the containers this checkout (and worker) owns,
  e.g. for a reaper that removes leaked containers without touching a
  concurrent session's.
* :func:`worker_id`, :data:`PID_FILE`, :data:`DONE_FILE` — the pieces an
  application needs to run the save protocol itself (``wrapper = false``).

The active ``ContainerCovPlugin`` is a process-local singleton
(``_active_plugin``) populated at ``pytest_configure`` time via
:func:`_register_active_plugin`. pytest-xdist workers are separate processes,
so each worker has its own plugin and its own owned containers.
"""

import warnings
from pathlib import Path
from typing import TYPE_CHECKING

from pytest_cov_container import config as _config
from pytest_cov_container.ownership import worker_id
from pytest_cov_container.protocol import DONE_FILE, PID_FILE

if TYPE_CHECKING:
    from pytest_cov_container.models import ContainerInfo
    from pytest_cov_container.plugin import ContainerCovPlugin

__all__ = [
    "DONE_FILE",
    "PID_FILE",
    "collect_container_coverage",
    "container_env",
    "owned_containers",
    "worker_id",
]

_active_plugin: "ContainerCovPlugin | None" = None


def _register_active_plugin(plugin: "ContainerCovPlugin | None") -> None:
    """Internal hook: ``ContainerCovPlugin`` calls this in pytest_configure
    so :func:`collect_container_coverage` can dispatch without a config arg.
    Set to ``None`` at unconfigure to surface stale-call warnings."""
    global _active_plugin
    _active_plugin = plugin


def collect_container_coverage() -> int:
    """Save and collect coverage from the running containers this process owns.

    Call this before stopping the containers. Returns the number of data files
    collected (0 when the plugin is inactive). With ``required``, collecting
    nothing raises ``RuntimeError``.

    Example::

        from pytest_cov_container import collect_container_coverage


        @pytest.fixture(scope="session")
        def sam_api():
            proc = start_sam(...)
            yield SAM_URL
            collect_container_coverage()
            proc.terminate()
    """
    if _active_plugin is None:
        warnings.warn(
            "pytest-cov-container is not active. "
            "Ensure pytest-cov is running (--cov) and "
            "[tool.pytest-cov-container] is configured.",
            UserWarning,
            stacklevel=2,
        )
        return 0
    return _active_plugin.collect_from_running()


def _load(rootpath: Path) -> "_config.PluginConfig | None":
    return _config.load_config(rootpath / "pyproject.toml")


def container_env(rootpath: Path) -> dict[str, str]:
    """Env vars to pass into the containers under test.

    Always the worker marker (``<worker_env>=<worker_id()>``) when
    ``worker_env`` is configured, so ownership works even with coverage off;
    plus the coverage bootstrap (``COVERAGE_PROCESS_START`` for Python) only
    while the plugin is active — without the injected rcfile it would point at
    nothing.

    ``sam local --env-vars`` only overrides variables the template already
    declares: declare each of these (empty) in ``template.yaml``, or sam drops
    them silently.
    """
    if _active_plugin is not None:
        return _active_plugin.container_env()
    cfg = _load(rootpath)
    if cfg is None or not cfg.worker_env:
        return {}
    return {cfg.worker_env: worker_id()}


def owned_containers(rootpath: Path, *, all_workers: bool = False) -> "list[ContainerInfo]":
    """Containers (any state) this checkout owns, per ``[tool.pytest-cov-container]``.

    ``all_workers`` ignores the worker marker (the mount check stays), for a
    pre-flight sweep of a previous run's leftovers. Works with the plugin
    inactive. Raises ``docker.errors.DockerException`` when the daemon is
    unreachable.
    """
    from pytest_cov_container import ownership
    from pytest_cov_container.docker_backend import DockerBackend

    cfg = _load(rootpath)
    if cfg is None:
        return []
    backend = _active_plugin.backend if _active_plugin is not None else DockerBackend()
    return ownership.find_owned(backend, cfg, rootpath, all_workers=all_workers)
