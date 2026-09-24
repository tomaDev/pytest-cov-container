from dataclasses import dataclass, field
from pathlib import Path

import tomllib

from pytest_cov_container.models import DriverConfig

# SAM CLI 1.165+ labels every container ``sam local`` starts for a function.
SAM_LAMBDA_LABEL = "sam.cli.container.type=lambda"
DEFAULT_BUILD_DIR = ".aws-sam/build/ApiFunction"


@dataclass
class PluginConfig:
    image_pattern: str | list[str] | None = None
    # Built for ``sam local``: the defaults select SAM's Lambda containers, and
    # ``load_config`` defaults ``mount_prefix`` to the SAM build root. An empty
    # string switches either filter off.
    label: str | None = SAM_LAMBDA_LABEL
    language: str = "python"
    enabled: bool = True
    path_mapping: dict[str, str] = field(default_factory=dict)
    driver_config: DriverConfig | None = None
    # Ownership keys — see ``pytest_cov_container.ownership``.
    mount_prefix: str | None = None
    worker_env: str | None = None
    # Fail the run when a collection pass gets no coverage data.
    required: bool = False

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
    raise ValueError(f"[tool.pytest-cov-container] {key} must be true or false, got {value!r}")


def load_config(pyproject_path: Path) -> PluginConfig | None:
    if not pyproject_path.exists():
        return None

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    tool_config = data.get("tool", {}).get("pytest-cov-container")
    if tool_config is None:
        return None

    path_mapping = {
        str(k): str(v) for k, v in tool_config.get("path_mapping", {}).items()
    }

    plugin_config = PluginConfig(
        image_pattern=tool_config.get("image_pattern"),
        label=tool_config.get("label", SAM_LAMBDA_LABEL),
        language=tool_config.get("language", "python"),
        enabled=tool_config.get("enabled", True),
        path_mapping=path_mapping,
        worker_env=tool_config.get("worker_env"),
        required=bool(_optional_bool(tool_config, "required")),
    )

    driver_section = tool_config.get(plugin_config.language, {})
    entrypoint = driver_section.get("entrypoint")
    if entrypoint == "":
        raise ValueError(
            "[tool.pytest-cov-container.python].entrypoint is empty. "
            "Remove the field to use convention discovery, or set a non-empty command."
        )
    wrapper = _optional_bool(driver_section, "wrapper")
    if wrapper is False and entrypoint is not None:
        raise ValueError(
            "[tool.pytest-cov-container.python]: entrypoint needs the wrapper; "
            "remove entrypoint or wrapper = false."
        )
    relative_files = _optional_bool(driver_section, "relative_files")
    build_dir = driver_section.get("build_dir", DEFAULT_BUILD_DIR)
    # SAM builds each function into ``<build root>/<LogicalId>``, so the build
    # root covers every function of this checkout and no other checkout's.
    plugin_config.mount_prefix = tool_config.get("mount_prefix", Path(build_dir).parent.as_posix())
    plugin_config.driver_config = DriverConfig(
        build_dir=build_dir,
        entrypoint=entrypoint,
        path_mapping=path_mapping,
        wrapper=wrapper is not False,
        source_dir=driver_section.get("source_dir"),
        container_root=driver_section.get("container_root", "/var/task"),
        branch=_optional_bool(driver_section, "branch"),
        relative_files=relative_files is not False,
    )

    return plugin_config
