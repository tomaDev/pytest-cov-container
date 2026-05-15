# SPDX-FileCopyrightText: 2026-present tomaDev <genins21@gmail.com>
#
# SPDX-License-Identifier: MIT
"""Public API surface for pytest-cov-container.

The package exposes a single user-facing helper: ``collect_container_coverage``.
The implementation looks up the active ``ContainerCovPlugin`` via a
process-local singleton (``_active_plugin``) populated at ``pytest_configure``
time by ``plugin.pytest_configure`` via :func:`_register_active_plugin`. This
is the standard pytest plugin idiom — pytest itself uses module-level state
for plugin registration, and pytest-xdist workers each get their own process
(and therefore their own module state), so cross-worker leakage is not a
concern. Helper-fixture access (``request.config.pluginmanager.get_plugin``)
is the pytest-native alternative, but it requires injecting a fixture into
the caller; the singleton lets ``collect_container_coverage()`` work as a
plain function call from user code, which the public-API contract requires.
"""

import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pytest_cov_container.plugin import ContainerCovPlugin

_active_plugin: "ContainerCovPlugin | None" = None


def _register_active_plugin(plugin: "ContainerCovPlugin | None") -> None:
    """Internal hook: ``ContainerCovPlugin`` calls this in pytest_configure
    so :func:`collect_container_coverage` can dispatch without a config arg.
    Set to ``None`` on session teardown to surface stale-call warnings."""
    global _active_plugin
    _active_plugin = plugin


def collect_container_coverage() -> None:
    """Flush and collect coverage from running containers.

    Call this before stopping containers to capture mid-session coverage.

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
        return
    _active_plugin.collect_from_running()
