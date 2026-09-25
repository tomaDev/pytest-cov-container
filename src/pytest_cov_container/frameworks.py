"""Framework presets: ``framework = "<name>"`` fills in what the framework decides.

A preset derives what a project would otherwise repeat by hand: which
functions run in containers, where each is built, where its source lives and
where the runtime mounts it, and how the framework's containers are
recognised. Any key the project sets explicitly wins. One framework is
supported today; the next consumer gets its own preset.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# SAM CLI 1.165+ labels every container ``sam local`` starts for a function.
SAM_LAMBDA_LABEL = "sam.cli.container.type=lambda"
_SAM_BUILD_ROOT = ".aws-sam/build"
_SAM_TASK_ROOT = "/var/task"
_SAM_LAYER_ROOT = "/opt"


@dataclass(frozen=True)
class SourceMapping:
    """A host source directory and where the container sees its files."""

    host_dir: str  # relative to rootdir, or absolute
    container_dir: str


@dataclass(frozen=True)
class FunctionTarget:
    """One build dir measured in its containers.

    SAM builds functions that share their code once, so a build dir can serve
    several functions; their containers report the build dir's ``name``.
    """

    name: str
    build_dir: str  # relative to rootdir, or absolute
    container_root: str
    # The code first, then the local layers (merged over ``functions``).
    mappings: tuple[SourceMapping, ...]
    functions: tuple[str, ...] = ()


@dataclass(frozen=True)
class FrameworkDefaults:
    label: str | None
    mount_prefix: str | None
    targets: tuple[FunctionTarget, ...]


class _CfnLoader(yaml.SafeLoader):
    """Safe YAML loader that tolerates CloudFormation's short-form intrinsics.

    ``!Ref X`` loads as ``{"Ref": "X"}`` (layers are referenced that way);
    every other tag (``!Sub``, ``!GetAtt``…) loads as ``None``. The preset reads
    only literal values and local references, never resolved ones.
    """


_CfnLoader.add_constructor("!Ref", lambda loader, node: {"Ref": loader.construct_scalar(node)})
_CfnLoader.add_multi_constructor("!", lambda _loader, _suffix, _node: None)


def _load_template(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        msg = f"SAM template {path} not found; set `template` in [tool.pytest-cov-container]"
        raise FileNotFoundError(msg)
    loader = _CfnLoader(path.read_text())
    try:
        return loader.get_single_data() or {}
    finally:
        loader.dispose()


def _relative_to_root(path: Path, rootpath: Path) -> str:
    try:
        return path.resolve().relative_to(rootpath.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _layer_root(layer: Mapping[str, Any]) -> str:
    """Where a layer's ``ContentUri`` files land in the container.

    ``BuildMethod: python3.x`` builds them into ``python/``; any other build
    (none, ``makefile``) keeps the ``ContentUri`` tree, which then holds ``python/``.
    """
    build_method = (layer.get("Metadata") or {}).get("BuildMethod")
    if isinstance(build_method, str) and build_method.startswith("python"):
        return f"{_SAM_LAYER_ROOT}/python"
    return _SAM_LAYER_ROOT


class _SamTemplate:
    def __init__(self, path: Path, rootpath: Path):
        self.path = path
        self.rootpath = rootpath
        template = _load_template(path)
        self.resources: Mapping[str, Any] = template.get("Resources") or {}
        self.globals: Mapping[str, Any] = (template.get("Globals") or {}).get("Function") or {}

    def _prop(self, properties: Mapping[str, Any], key: str) -> Any:
        return properties.get(key, self.globals.get(key))

    def _source(self, uri: str) -> str:
        return _relative_to_root(self.path.parent / uri, self.rootpath)

    def ineligible_reason(self, name: str) -> str | None:
        """Why ``name`` cannot be measured, or None when it can."""
        resource = self.resources.get(name)
        if not isinstance(resource, Mapping) or resource.get("Type") != "AWS::Serverless::Function":
            return f"is not an AWS::Serverless::Function in {self.path}"
        properties = resource.get("Properties") or {}
        if self._prop(properties, "PackageType") == "Image":
            return "is an image function; only zip functions are measured"
        runtime = self._prop(properties, "Runtime")
        if not isinstance(runtime, str) or not runtime.startswith("python"):
            return f"runtime {runtime!r} is not Python"
        if not isinstance(self._prop(properties, "CodeUri"), str):
            return "has no local CodeUri"
        return None

    def functions(self) -> list[str]:
        return [name for name in self.resources if self.ineligible_reason(name) is None]

    def _layer_mappings(self, properties: Mapping[str, Any]) -> list[SourceMapping]:
        mappings = []
        # SAM appends a function's Layers to the Globals ones.
        for layer in [*(self.globals.get("Layers") or []), *(properties.get("Layers") or [])]:
            ref = layer.get("Ref") if isinstance(layer, Mapping) else None
            resource = self.resources.get(ref) if isinstance(ref, str) else None
            if not isinstance(resource, Mapping) or resource.get("Type") != "AWS::Serverless::LayerVersion":
                continue
            content = (resource.get("Properties") or {}).get("ContentUri")
            if isinstance(content, str):
                mappings.append(SourceMapping(self._source(content), _layer_root(resource)))
        return mappings

    def mappings(self, name: str) -> list[SourceMapping]:
        properties = self.resources[name].get("Properties") or {}
        return [
            SourceMapping(self._source(self._prop(properties, "CodeUri")), _SAM_TASK_ROOT),
            *self._layer_mappings(properties),
        ]


def _build_dirs(built_template: Path, names: list[str]) -> dict[str, str]:
    """Each function's build dir name, from ``sam build``'s own template.

    Its ``CodeUri`` is the dir under the build root (``KBSyncFunction-Shared``
    for functions sharing code). Before the first build, the logical id.
    """
    resources: Mapping[str, Any] = {}
    if built_template.is_file():
        resources = _load_template(built_template).get("Resources") or {}
    dirs = {}
    for name in names:
        code_uri = ((resources.get(name) or {}).get("Properties") or {}).get("CodeUri")
        dirs[name] = code_uri if isinstance(code_uri, str) else name
    return dirs


def _targets(template: _SamTemplate, names: list[str], build_root: str, rootpath: Path) -> tuple[FunctionTarget, ...]:
    grouped: dict[str, list[str]] = {}
    for name, build_dir in _build_dirs(rootpath / build_root / "template.yaml", names).items():
        grouped.setdefault(build_dir, []).append(name)
    targets = []
    for build_dir, functions in grouped.items():
        mappings = dict.fromkeys(mapping for name in functions for mapping in template.mappings(name))
        targets.append(
            FunctionTarget(
                name=build_dir,
                build_dir=f"{build_root}/{build_dir}",
                container_root=_SAM_TASK_ROOT,
                mappings=tuple(mappings),
                functions=tuple(functions),
            )
        )
    return tuple(targets)


def aws_sam(tool_config: Mapping[str, Any], rootpath: Path) -> FrameworkDefaults:
    """``sam local``: every zip Python function in the template, or ``functions``.

    Reads ``template.yaml`` at rootdir (``template`` overrides). Each function
    is measured from its dir under the build root (``build_dir``, default
    ``.aws-sam/build``; the built template names the dir) with its ``CodeUri``
    mounted at ``/var/task`` and its local layers at ``/opt``.
    """
    template = _SamTemplate(rootpath / tool_config.get("template", "template.yaml"), rootpath)
    build_root = tool_config.get("build_dir", _SAM_BUILD_ROOT)
    names = tool_config.get("functions")
    if names is None:
        names = template.functions()
        if not names:
            msg = f"no zip Python function with a local CodeUri in {template.path}"
            raise ValueError(msg)
    for name in names:
        reason = template.ineligible_reason(name)
        if reason is not None:
            msg = f"[tool.pytest-cov-container] functions: {name!r} {reason}"
            raise ValueError(msg)
    return FrameworkDefaults(
        label=SAM_LAMBDA_LABEL,
        # Every function's build dir sits under the build root, so it covers
        # this checkout's containers and no other checkout's.
        mount_prefix=build_root,
        targets=_targets(template, list(names), build_root, rootpath),
    )


PRESETS = {"aws-sam": aws_sam}


def defaults(framework: str | None, tool_config: Mapping[str, Any], rootpath: Path) -> FrameworkDefaults:
    supported = ", ".join(sorted(PRESETS))
    if not framework:
        msg = f"[tool.pytest-cov-container] framework is required (supported: {supported})"
        raise ValueError(msg)
    preset = PRESETS.get(framework)
    if preset is None:
        msg = f"[tool.pytest-cov-container] framework {framework!r} is not supported (supported: {supported})"
        raise ValueError(
            msg
        )
    return preset(tool_config, rootpath)
