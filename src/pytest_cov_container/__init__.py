# SPDX-FileCopyrightText: 2026-present tomaDev <genins21@gmail.com>
#
# SPDX-License-Identifier: MIT
import warnings
from typing import Any

_active_plugin: Any = None


def collect_container_coverage():
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
