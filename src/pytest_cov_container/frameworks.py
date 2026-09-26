"""Framework presets: ``framework = "<name>"`` fills in what the framework decides.

A preset derives what a project would otherwise repeat by hand: which
functions run in containers, where each is built, where its source lives and
where the runtime mounts it, and how the framework's containers are
recognised. Any key the project sets explicitly wins. One framework is
supported today; the next consumer gets its own preset.
"""

import re
import tomllib
import warnings
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
# Directory names never measured: test trees and caches are not deployed, and
# dot-dirs (``.venv``, ``.aws-sam``) hold third-party or build copies.
_SOURCE_SKIP_DIRS = frozenset({"__pycache__", "tests"})


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
    # The code first, then the local layers, then the local uv packages (merged over ``functions``).
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


def source_files(host_dir: Path) -> list[Path]:
    """The measured ``*.py`` files under ``host_dir``, relative to it."""
    files = []
    for path in sorted(host_dir.rglob("*.py")):
        rel = path.relative_to(host_dir)
        if not any(part in _SOURCE_SKIP_DIRS or part.startswith(".") for part in rel.parts[:-1]):
            files.append(rel)
    return files


def _join(base: str, sub: tuple[str, ...]) -> str:
    return "/".join([base.rstrip("/"), *sub])


def _common_tail(a: tuple[str, ...], b: tuple[str, ...]) -> int:
    n = 0
    while n < min(len(a), len(b)) and a[-1 - n] == b[-1 - n]:
        n += 1
    return n


def _read_sources(source: Path) -> list[tuple[tuple[str, ...], bytes]]:
    """Each measured file under ``source``: its path parts, relative to ``source``, and its bytes."""
    return [(rel.parts, (source / rel).read_bytes()) for rel in source_files(source)]


def _built_matches(
    sources: list[tuple[tuple[str, ...], bytes]], built: Path, candidates: list[Path] | None = None
) -> list[tuple[tuple[str, ...], tuple[str, ...], int]]:
    """Where ``sam build`` put each of the ``sources`` files in ``built``.

    ``(source path, built path, common tail)`` per matched file: the built file
    with the same bytes and the longest common path tail (a vendored
    dependency may share its name). ``candidates`` (paths relative to
    ``built``) narrows the search; by default every ``*.py`` in ``built``.
    """
    by_name: dict[str, list[Path]] = {}
    for path in candidates if candidates is not None else (p.relative_to(built) for p in built.rglob("*.py")):
        by_name.setdefault(path.name, []).append(path)
    matches = []
    for rel, content in sources:
        # Longest tail first, so a common name (``__init__.py``) reads few files.
        ranked = sorted((-_common_tail(rel, c.parts), c) for c in by_name.get(rel[-1], ()))
        for negative_tail, candidate in ranked:
            built_path = built / candidate
            # A RECORD-listed candidate need not exist (e.g. stripped after install).
            if built_path.is_file() and built_path.read_bytes() == content:
                matches.append((rel, candidate.parts, -negative_tail))
                break
    return matches


def _built_layout(sources: list[tuple[tuple[str, ...], bytes]], built: Path) -> list[tuple[tuple[str, ...], tuple[str, ...]]] | None:
    """How ``sam build`` placed ``source``'s files in ``built``: ``(source subdir, built subdir)`` pairs.

    What precedes each matched file's common tail on each side is the pair.
    None when no source file is in the build.
    """
    matches = _built_matches(sources, built)
    pairs = {(rel[:-tail], candidate[:-tail]) for rel, candidate, tail in matches}
    return sorted(pairs) or None


def _package_dirs(matches: list[tuple[tuple[str, ...], tuple[str, ...], int]]) -> list[tuple[tuple[str, ...], tuple[str, ...]]]:
    """Each top-level package dir the ``matches`` fall in: ``(source dir, built dir)`` pairs.

    Unlike a layer's layout, a package shares ``/var/task`` with the function's
    code, so it maps by its own package dirs, never by the build root.
    """
    return sorted(
        {
            (rel[: len(rel) - tail + 1], candidate[: len(candidate) - tail + 1])
            for rel, candidate, tail in matches
            # A module file at the top of the build has no package dir of its own.
            if tail > 1
        }
    )


def _local_packages(code_dir: Path) -> list[tuple[str, str]]:
    """``(name, source dir as written)`` of each local package in ``code_dir``'s ``uv.lock``.

    The lock lists every package the function resolved, direct or not, with
    ``source = { directory = "..." }`` (or ``editable``) for a local one; the
    project itself (``"."``) is its own code.
    """
    lock = code_dir / "uv.lock"
    if not lock.is_file():
        return []
    with lock.open("rb") as f:
        packages = tomllib.load(f).get("package", [])
    local = []
    for package in packages:
        source = package.get("source") or {}
        path = source.get("directory") or source.get("editable")
        if isinstance(path, str) and path != "." and (code_dir / path).is_dir():
            local.append((package["name"], path))
    return local


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "_", name).lower()


def _installed_files(built: Path, name: str) -> list[Path] | None:
    """The ``*.py`` files the build installed for distribution ``name`` (its ``RECORD``), or None if not installed."""
    for info in sorted(built.glob("*.dist-info")):
        if _normalized(info.name.removesuffix(".dist-info").rpartition("-")[0]) != _normalized(name):
            continue
        record = info / "RECORD"
        if not record.is_file():
            return []
        rows = (line.split(",", 1)[0] for line in record.read_text(encoding="utf-8").splitlines())
        return [Path(row) for row in rows if row.endswith(".py") and not row.startswith("..")]
    return None


def _layer_root(layer: Mapping[str, Any]) -> str:
    """Where an unbuilt layer's ``ContentUri`` files will land in the container.

    ``BuildMethod: python3.x`` builds them into ``python/``; any other build
    (none, ``makefile``) keeps the ``ContentUri`` tree, which then holds ``python/``.
    """
    build_method = (layer.get("Metadata") or {}).get("BuildMethod")
    if isinstance(build_method, str) and build_method.startswith("python"):
        return f"{_SAM_LAYER_ROOT}/python"
    return _SAM_LAYER_ROOT


class _SamTemplate:
    def __init__(self, path: Path, rootpath: Path, build_root: str):
        self.path = path
        self.rootpath = rootpath
        template = _load_template(path)
        self.resources: Mapping[str, Any] = template.get("Resources") or {}
        self.globals: Mapping[str, Any] = (template.get("Globals") or {}).get("Function") or {}
        self.build_root = rootpath / build_root
        # Every function that uses a package or layer reads the same sources.
        self._sources: dict[Path, list[tuple[tuple[str, ...], bytes]]] = {}
        built_template = self.build_root / "template.yaml"
        # ``sam build``'s own template: where it put each function and layer.
        self.built: Mapping[str, Any] = {}
        if built_template.is_file():
            self.built = _load_template(built_template).get("Resources") or {}

    def built_uri(self, name: str, key: str) -> str | None:
        uri = ((self.built.get(name) or {}).get("Properties") or {}).get(key)
        return uri if isinstance(uri, str) else None

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
            if not isinstance(ref, str):
                continue
            resource = self.resources.get(ref)
            if not isinstance(resource, Mapping) or resource.get("Type") != "AWS::Serverless::LayerVersion":
                continue
            content = (resource.get("Properties") or {}).get("ContentUri")
            if isinstance(content, str):
                mappings.extend(self._layer_placement(ref, resource, content))
        return mappings

    def _layer_placement(self, name: str, resource: Mapping[str, Any], content: str) -> list[SourceMapping]:
        """The layer's source, placed where ``sam build`` put it; before a build, by its build method."""
        source = self._source(content)
        fallback = [SourceMapping(source, _layer_root(resource))]
        built_uri = self.built_uri(name, "ContentUri")
        built = self.build_root / built_uri if built_uri else None
        host = Path(source) if Path(source).is_absolute() else self.rootpath / source
        if built is None or not built.is_dir() or not host.is_dir():
            return fallback
        layout = _built_layout(self._read(host), built)
        if layout is None:
            if source_files(host):
                warnings.warn(
                    f"pytest-cov-container: {name}: none of its source files are in its build output {built}; "
                    f"assuming they sit at {fallback[0].container_dir}",
                    UserWarning,
                    stacklevel=2,
                )
            return fallback
        return [SourceMapping(_join(source, host_sub), _join(_SAM_LAYER_ROOT, built_sub)) for host_sub, built_sub in layout]

    def _read(self, source: Path) -> list[tuple[tuple[str, ...], bytes]]:
        if source not in self._sources:
            self._sources[source] = _read_sources(source)
        return self._sources[source]

    def _package_mappings(self, name: str, code: str) -> list[SourceMapping]:
        """The local packages in the function's ``uv.lock``, where ``sam build`` installed them.

        ``python-uv`` installs a local package into the function's build dir
        like any dependency, so its files sit under ``/var/task``; the
        package's ``RECORD`` lists them. Before a build there is nowhere to
        read that from, and nothing to measure.
        """
        built_uri = self.built_uri(name, "CodeUri")
        built = self.build_root / built_uri if built_uri else None
        if built is None or not built.is_dir():
            return []
        code_dir = Path(code) if Path(code).is_absolute() else self.rootpath / code
        mappings = []
        for package, path in _local_packages(code_dir):
            installed = _installed_files(built, package)
            if installed is None:  # locked but not shipped, e.g. a dev-group package
                continue
            host = code_dir / path
            sources = self._read(host)
            matches = _built_matches(sources, built, installed)
            unmapped = (
                "none of its source files' bytes match anything the build installed "
                "(e.g. an editable install ships a link, not the files)"
            )
            if matches:
                # A top-level module file has no package dir of its own to map.
                modules = sorted("/".join(rel) for rel, _, tail in matches if tail == 1)
                unmapped = f"top-level module files {modules} have no package dir to map" if modules else ""
            if unmapped and sources:
                warnings.warn(
                    f"pytest-cov-container: {name}: {package} ({path}) is installed but not measured: {unmapped}",
                    UserWarning,
                    stacklevel=2,
                )
            source = _relative_to_root(host, self.rootpath)
            mappings.extend(
                SourceMapping(_join(source, host_dir), _join(_SAM_TASK_ROOT, built_dir))
                for host_dir, built_dir in _package_dirs(matches)
            )
        return mappings

    def mappings(self, name: str) -> list[SourceMapping]:
        properties = self.resources[name].get("Properties") or {}
        code = self._source(self._prop(properties, "CodeUri"))
        return [
            SourceMapping(code, _SAM_TASK_ROOT),
            *self._layer_mappings(properties),
            *self._package_mappings(name, code),
        ]


def _build_dirs(template: _SamTemplate, names: list[str]) -> dict[str, str]:
    """Each function's build dir name, from ``sam build``'s own template.

    Its ``CodeUri`` is the dir under the build root (``KBSyncFunction-Shared``
    for functions sharing code). Before the first build, the logical id.
    """
    return {name: template.built_uri(name, "CodeUri") or name for name in names}


def _targets(template: _SamTemplate, names: list[str], build_root: str) -> tuple[FunctionTarget, ...]:
    grouped: dict[str, list[str]] = {}
    for name, build_dir in _build_dirs(template, names).items():
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
    mounted at ``/var/task`` and its local layers where ``sam build`` put
    them under ``/opt``.
    """
    build_root = tool_config.get("build_dir", _SAM_BUILD_ROOT)
    template = _SamTemplate(rootpath / tool_config.get("template", "template.yaml"), rootpath, build_root)
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
        targets=_targets(template, list(names), build_root),
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
