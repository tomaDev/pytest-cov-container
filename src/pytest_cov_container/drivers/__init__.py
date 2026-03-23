from importlib.metadata import entry_points

from pytest_cov_container.models import LanguageDriver


def discover_drivers() -> dict[str, LanguageDriver]:
    eps = entry_points(group="pytest_cov_container.drivers")
    drivers: dict[str, LanguageDriver] = {}
    for ep in eps:
        driver_class = ep.load()
        drivers[ep.name] = driver_class()
    return drivers


def get_driver(name: str) -> LanguageDriver:
    found = discover_drivers()
    if name not in found:
        available = ", ".join(sorted(found.keys()))
        msg = f"Unknown language driver '{name}'. Available: {available}"
        raise ValueError(msg)
    return found[name]
