import tomllib
from dataclasses import dataclass
from pathlib import Path

from pytest_cov_container import frameworks
from pytest_cov_container.frameworks import FunctionTarget

_SECTION = "[tool.pytest-cov-container]"


@dataclass
class PluginConfig:
    framework: str | None = None
    targets: tuple[FunctionTarget, ...] = ()
    image_pattern: str | list[str] | None = None
    label: str | None = None
    enabled: bool = True
    # Ownership keys — see ``pytest_cov_container.ownership``.
    mount_prefix: str | None = None
    worker_env: str | None = None
    # Fail the run when a collection pass gets no coverage data.
    required: bool = False
    # Branch coverage in the containers; ``None`` inherits the host's setting,
    # which it must match to combine.
    branch: bool | None = None
    # Where containers reach the sink (the host), and where the sink listens.
    # ``None``: detected from the Docker engine (``DockerBackend.host_endpoint``).
    sink_host: str | None = None
    sink_bind: str | None = None
    # The docker network the containers join, for that detection (default: bridge).
    docker_network: str | None = None

    @property
    def image_patterns(self) -> list[str]:
        if self.image_pattern is None:
            return []
        if isinstance(self.image_pattern, str):
            return [self.image_pattern]
        return list(self.image_pattern)


def _optional_bool(section: dict, key: str) -> bool | None:
    value = section.get(key)
    if value is None or isinstance(value, bool):
        return value
    msg = f"{_SECTION} {key} must be true or false, got {value!r}"
    raise ValueError(msg)


def load_config(pyproject_path: Path) -> PluginConfig | None:
    if not pyproject_path.exists():
        return None

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    tool_config = data.get("tool", {}).get("pytest-cov-container")
    if tool_config is None:
        return None

    framework = tool_config.get("framework")
    # The framework's defaults; every key set explicitly below wins over them.
    preset = frameworks.defaults(framework, tool_config, pyproject_path.parent)
    return PluginConfig(
        framework=framework,
        targets=preset.targets,
        image_pattern=tool_config.get("image_pattern"),
        label=tool_config.get("label", preset.label),
        enabled=tool_config.get("enabled", True),
        mount_prefix=tool_config.get("mount_prefix", preset.mount_prefix),
        worker_env=tool_config.get("worker_env"),
        required=bool(_optional_bool(tool_config, "required")),
        branch=_optional_bool(tool_config, "branch"),
        sink_host=tool_config.get("sink_host"),
        sink_bind=tool_config.get("sink_bind"),
        docker_network=tool_config.get("docker_network"),
    )
