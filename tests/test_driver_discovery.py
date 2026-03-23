import pytest

from pytest_cov_container import drivers
from pytest_cov_container.drivers.python import PythonDriver
from pytest_cov_container.models import LanguageDriver


class TestDiscoverDrivers:
    def test_discovers_python_driver(self):
        found = drivers.discover_drivers()
        assert "python" in found
        assert isinstance(found["python"], PythonDriver)

    def test_python_driver_satisfies_protocol(self):
        found = drivers.discover_drivers()
        assert isinstance(found["python"], LanguageDriver)


class TestGetDriver:
    def test_returns_python_driver(self):
        driver = drivers.get_driver("python")
        assert isinstance(driver, PythonDriver)

    def test_raises_for_unknown_driver(self):
        with pytest.raises(ValueError, match="Unknown language driver 'ruby'"):
            drivers.get_driver("ruby")
