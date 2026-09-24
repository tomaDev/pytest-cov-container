from pathlib import Path

import pytest

from pytest_cov_container import _boot, inject, protocol
from pytest_cov_container.frameworks import FunctionTarget, SourceMapping

WORKER = FunctionTarget(
    name="Worker",
    build_dir=".aws-sam/build/Worker",
    container_root="/var/task",
    mappings=(
        SourceMapping("src/worker", "/var/task"),
        SourceMapping("src/shared", "/opt"),
        SourceMapping("src/extra", "/opt"),
    ),
)


class TestIncludePatterns:
    def test_every_source_file_at_its_container_path(self, sam_project):
        assert inject.include_patterns(WORKER, sam_project) == [
            "/var/task/handler.py",
            "/opt/python/shared/util.py",
            "/opt/python/extra.py",
        ]

    def test_skips_tests_and_dot_dirs(self, sam_project):
        api = FunctionTarget("ApiFunction", ".aws-sam/build/ApiFunction", "/var/task", (SourceMapping("src/api", "/var/task"),))
        assert inject.include_patterns(api, sam_project) == ["/var/task/app.py"]

    def test_missing_source_dir(self, sam_project):
        broken = FunctionTarget("X", ".aws-sam/build/Worker", "/var/task", (SourceMapping("src/nope", "/var/task"),))
        with pytest.raises(FileNotFoundError, match="X: source directory"):
            inject.include_patterns(broken, sam_project)


def test_rcfile(sam_project):
    rc = inject.render_coveragerc(WORKER, rootpath=sam_project, branch=True)
    assert f"data_file = {protocol.DATA_FILE}\n" in rc
    assert "branch = true\n" in rc
    assert "parallel = true\n" in rc
    assert "    /var/task/handler.py\n" in rc


class TestInject:
    def test_writes_the_bootstrap(self, sam_project):
        inject.inject(WORKER, rootpath=sam_project, branch=False)
        build = sam_project / ".aws-sam/build/Worker"
        assert "branch = false" in (build / protocol.RCFILE).read_text()
        boot_dir = build / protocol.BOOT_DIR
        assert (boot_dir / f"{protocol.BOOT_MODULE}.py").read_text() == Path(_boot.__file__).read_text()
        assert (boot_dir / protocol.BOOT_CONFIG).read_text() == "Worker\n"
        assert (build / protocol.PTH_FILE).read_text() == (
            "/var/task/.cov_container\nimport _cov_container_boot; _cov_container_boot.main()\n"
        )

    def test_copies_pure_python_coverage(self, sam_project):
        inject.inject(WORKER, rootpath=sam_project, branch=False)
        copied = sam_project / ".aws-sam/build/Worker" / protocol.BOOT_DIR / "coverage"
        assert (copied / "__init__.py").is_file()
        assert not list(copied.rglob("*.so"))
        assert not list(copied.rglob("__pycache__"))

    def test_reinjecting_replaces_the_copy(self, sam_project):
        inject.inject(WORKER, rootpath=sam_project, branch=False)
        stale = sam_project / ".aws-sam/build/Worker" / protocol.BOOT_DIR / "coverage" / "stale.py"
        stale.write_text("")
        inject.inject(WORKER, rootpath=sam_project, branch=True)
        assert not stale.exists()
        assert not list((sam_project / ".aws-sam/build/Worker").glob(".*.tmp"))

    def test_missing_build_dir(self, sam_project):
        missing = FunctionTarget("Gone", ".aws-sam/build/Gone", "/var/task", WORKER.mappings)
        with pytest.raises(FileNotFoundError, match="Run 'sam build'"):
            inject.inject(missing, rootpath=sam_project, branch=False)


def test_boot_mirrors_the_protocol():
    # The boot runs in the container without the plugin, so it carries copies.
    assert _boot.RCFILE_ENV == protocol.RCFILE_ENV
    assert _boot.SINK_ENV == protocol.SINK_ENV
    assert _boot.BOOT_CONFIG == protocol.BOOT_CONFIG
    assert _boot.PID_FILE == protocol.PID_FILE
    assert _boot.ID_HEADER == protocol.ID_HEADER
    assert _boot.FUNCTION_HEADER == protocol.FUNCTION_HEADER
