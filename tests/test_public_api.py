from unittest.mock import MagicMock

import pytest

import pytest_cov_container


class TestCollectContainerCoverage:
    def teardown_method(self):
        pytest_cov_container._register_active_plugin(None)

    def test_warns_when_plugin_not_active(self):
        pytest_cov_container._register_active_plugin(None)
        with pytest.warns(UserWarning, match="not active"):
            pytest_cov_container.collect_container_coverage()

    def test_calls_collect_from_running_when_active(self):
        mock_plugin = MagicMock()
        pytest_cov_container._register_active_plugin(mock_plugin)
        pytest_cov_container.collect_container_coverage()
        mock_plugin.collect_from_running.assert_called_once()
