"""Inject the coverage bootstrap into a function's build dir (see ``protocol``)."""

import os
import shutil
from pathlib import Path

import coverage

from pytest_cov_container import frameworks, protocol
from pytest_cov_container.frameworks import FunctionTarget

_BOOT_SOURCE = Path(__file__).with_name("_boot.py")


def _absolute(path: str, rootpath: Path) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else rootpath / candidate


def include_patterns(target: FunctionTarget, rootpath: Path) -> list[str]:
    """Every source ``*.py`` of ``target``, at its container path.

    Listing the files keeps the vendored dependencies that share the build dir
    (and the injected coverage copy) unmeasured.
    """
    patterns = []
    for mapping in target.mappings:
        host_dir = _absolute(mapping.host_dir, rootpath)
        if not host_dir.is_dir():
            msg = f"{target.name}: source directory {host_dir} does not exist"
            raise FileNotFoundError(msg)
        root = mapping.container_dir.rstrip("/")
        patterns.extend(f"{root}/{rel.as_posix()}" for rel in frameworks.source_files(host_dir))
    return patterns


def render_coveragerc(target: FunctionTarget, *, rootpath: Path, branch: bool) -> str:
    """The ``.coveragerc`` the container's coverage reads.

    ``branch`` must equal the host's setting: a statement-only container
    dataset cannot combine with branch data.
    """
    include = "\n    ".join(include_patterns(target, rootpath))
    return (
        "[run]\n"
        f"data_file = {protocol.DATA_FILE}\n"
        f"branch = {str(branch).lower()}\n"
        "parallel = true\n"
        "include =\n"
        f"    {include}\n"
    )


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` so a concurrent reader (a starting container) sees old or new, never partial."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    tmp.replace(path)


def _copy_coverage(dest: Path) -> None:
    """A pure-Python copy of the host's ``coverage`` package.

    Compiled extensions are left out: they are built for the host, and coverage
    falls back to its Python cores (``sys.monitoring`` on 3.12+) without them.
    """
    source = Path(coverage.__file__).parent
    tmp = dest.with_name(f".{dest.name}.{os.getpid()}.tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(source, tmp, ignore=shutil.ignore_patterns("__pycache__", "*.so", "*.pyd"))
    shutil.rmtree(dest, ignore_errors=True)
    tmp.replace(dest)


def inject(target: FunctionTarget, *, rootpath: Path, branch: bool) -> list[Path]:
    """Write the rcfile, the boot dir and the ``.pth`` hook into ``target``'s build dir."""
    build_dir = _absolute(target.build_dir, rootpath)
    if not build_dir.is_dir():
        msg = f"Build directory {build_dir} does not exist. Run 'sam build' before running tests."
        raise FileNotFoundError(msg)
    boot_dir = build_dir / protocol.BOOT_DIR
    boot_dir.mkdir(exist_ok=True)
    _copy_coverage(boot_dir / "coverage")
    boot_module = boot_dir / f"{protocol.BOOT_MODULE}.py"
    _atomic_write(boot_module, _BOOT_SOURCE.read_text())
    boot_config = boot_dir / protocol.BOOT_CONFIG
    _atomic_write(boot_config, f"{target.name}\n")
    rcfile = build_dir / protocol.RCFILE
    _atomic_write(rcfile, render_coveragerc(target, rootpath=rootpath, branch=branch))
    pth = build_dir / protocol.PTH_FILE
    boot_path = f"{target.container_root.rstrip('/')}/{protocol.BOOT_DIR}"
    _atomic_write(pth, f"{boot_path}\nimport {protocol.BOOT_MODULE}; {protocol.BOOT_MODULE}.main()\n")
    return [rcfile, boot_module, boot_config, pth, boot_dir / "coverage"]
