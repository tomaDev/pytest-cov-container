from dataclasses import dataclass, field
from pathlib import Path

import tomllib

from pytest_cov_container.models import DriverConfig


@dataclass
class PluginConfig:
    image_pattern: str | None = None
    label: str | None = None
    language: str = "python"
    enabled: bool = True
    path_mapping: dict[str, str] = field(default_factory=dict)
    driver_config: DriverConfig | None = None


def load_config(pyproject_path: Path) -> PluginConfig | None:
    if not pyproject_path.exists():
        return None

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    tool_config = data.get("tool", {}).get("pytest-cov-container")
    if tool_config is None:
        return None

    path_mapping = {str(k): str(v) for k, v in tool_config.get("path_mapping", {}).items()}

    plugin_config = PluginConfig(
        image_pattern=tool_config.get("image_pattern"),
        label=tool_config.get("label"),
        language=tool_config.get("language", "python"),
        enabled=tool_config.get("enabled", True),
        path_mapping=path_mapping,
    )

    driver_section = tool_config.get(plugin_config.language, {})
    plugin_config.driver_config = DriverConfig(
        build_dir=driver_section.get("build_dir", ".aws-sam/build/ApiFunction"),
        entrypoint=driver_section.get("entrypoint", "uvicorn app:app --host 0.0.0.0 --port 8080"),
        path_mapping=path_mapping,
    )

    return plugin_config
