import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import coverage
import pytest
from coverage.data import CoverageData

from pytest_cov_container.config import PluginConfig
from pytest_cov_container.drivers.python import PythonDriver
from pytest_cov_container.models import ContainerInfo, DriverConfig
from pytest_cov_container.plugin import ContainerCovPlugin


@pytest.fixture
def plugin_config():
    return PluginConfig(
        image_pattern="samcli/lambda*",
        language="python",
        enabled=True,
        driver_config=DriverConfig(build_dir=".aws-sam/build/ApiFunction"),
    )


@pytest.fixture
def make_plugin(plugin_config, tmp_path, monkeypatch):
    """A plugin with a mocked docker backend; pytest-cov's data file is
    ``tmp_path/.coverage`` (the outer run's own Coverage is hidden)."""
    monkeypatch.setattr(coverage.Coverage, "current", classmethod(lambda cls: None))
    monkeypatch.setenv("COVERAGE_FILE", str(tmp_path / ".coverage"))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)

    def make(**kwargs):
        with patch("pytest_cov_container.plugin.DockerBackend"):
            return ContainerCovPlugin(plugin_config, tmp_path, **kwargs)

    return make


def _container(cid="c0", status="running"):
    return ContainerInfo(id=cid, name=f"sam-{cid}", image="samcli/lambda:3.12", labels={}, status=status)


def _data_file(path: Path, lines: dict[str, list[int]]) -> Path:
    data = CoverageData(basename=str(path))
    data.add_lines(lines)
    data.close()
    return path


def _stub_collect(plugin, files_by_id: dict[str, list[Path]]):
    plugin.driver = MagicMock()
    plugin.driver.collect.side_effect = lambda backend, container, dest, cfg: files_by_id.get(container.id, [])


class TestSessionStart:
    def test_injects_when_build_dir_exists(self, make_plugin, plugin_config, tmp_path):
        build_dir = tmp_path / ".aws-sam" / "build" / "ApiFunction"
        build_dir.mkdir(parents=True)
        plugin_config.driver_config.wrapper = False
        plugin = make_plugin()
        session = MagicMock()
        session.config.rootpath = tmp_path
        plugin.pytest_sessionstart(session)
        assert plugin.injection_result is not None
        assert (build_dir / ".coveragerc").exists()

    def test_raises_when_build_dir_missing(self, make_plugin, tmp_path):
        plugin = make_plugin()
        session = MagicMock()
        session.config.rootpath = tmp_path
        with pytest.raises(FileNotFoundError, match="does not exist"):
            plugin.pytest_sessionstart(session)

    def test_xdist_worker_does_not_inject(self, make_plugin, tmp_path):
        # The controller injected before spawning workers; a worker rewriting
        # the files could race a sibling's starting container.
        plugin = make_plugin(is_worker=True)
        session = MagicMock()
        session.config.rootpath = tmp_path
        plugin.pytest_sessionstart(session)  # build dir missing, yet no raise
        assert plugin.injection_result is None


class TestShardHandoff:
    def test_shard_is_a_suffix_file_of_pytest_covs_data_file(self, make_plugin, tmp_path):
        plugin = make_plugin()
        src = _data_file(tmp_path / "extracted", {"/var/task/app.py": [1, 2]})
        plugin._write_shard(src)
        shards = list(tmp_path.glob(".coverage.container-main-*"))
        assert len(shards) == 1
        assert not src.exists()
        data = CoverageData(basename=str(shards[0]))
        data.read()
        assert data.measured_files() == {"/var/task/app.py"}
        assert not list(tmp_path.glob("*.tmp"))

    def test_shard_names_carry_the_worker_id(self, make_plugin, tmp_path, monkeypatch):
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw2")
        plugin = make_plugin()
        plugin._write_shard(_data_file(tmp_path / "x", {"/var/task/a.py": [1]}))
        assert list(tmp_path.glob(".coverage.container-gw2-*"))

    def test_path_mapping_remaps_container_paths(self, make_plugin, plugin_config, tmp_path):
        host_file = tmp_path / "src" / "api" / "app.py"
        host_file.parent.mkdir(parents=True)
        host_file.write_text("x = 1\n")
        plugin_config.path_mapping = {"src/api": "/var/task"}
        plugin = make_plugin()
        plugin._write_shard(_data_file(tmp_path / "x", {"/var/task/app.py": [1]}))
        (shard,) = tmp_path.glob(".coverage.container-*")
        data = CoverageData(basename=str(shard))
        data.read()
        assert data.measured_files() == {str(host_file)}

    def test_combines_with_host_data_via_coverage_combine(self, make_plugin, tmp_path):
        # What pytest-cov's finish does: combine() picks up <data_file>.* files.
        plugin = make_plugin()
        _data_file(tmp_path / ".coverage.host.1.abc", {"/host/mod.py": [1]})
        plugin._write_shard(_data_file(tmp_path / "x", {"/var/task/app.py": [3]}))
        cov = coverage.Coverage(data_file=str(tmp_path / ".coverage"))
        cov.combine()
        assert cov.get_data().measured_files() == {"/host/mod.py", "/var/task/app.py"}


class TestExplicitCollect:
    def test_collects_running_owned_containers_only(self, make_plugin, tmp_path):
        plugin = make_plugin()
        plugin.find_owned = lambda **_: [_container("run"), _container("gone", status="exited")]
        _stub_collect(plugin, {"run": [_data_file(tmp_path / "f1", {"/var/task/a.py": [1]})]})
        assert plugin.collect_from_running() == 1
        assert [c.args[1].id for c in plugin.driver.collect.call_args_list] == ["run"]
        assert plugin.collected_ids == {"run"}

    def test_required_raises_when_nothing_collected(self, make_plugin, plugin_config, monkeypatch):
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
        plugin_config.worker_env = "MARK"
        plugin = make_plugin(required=True)
        plugin.find_owned = lambda **_: []
        _stub_collect(plugin, {})
        with pytest.raises(RuntimeError, match="MARK=gw1"):
            plugin.collect_from_running()

    def test_required_error_names_the_label_filter(self, make_plugin, plugin_config):
        plugin_config.label = "sam.cli.container.type=lambda"
        plugin = make_plugin(required=True)
        plugin.find_owned = lambda **_: []
        with pytest.raises(RuntimeError, match="labelled sam.cli.container.type=lambda"):
            plugin.collect_from_running()

    def test_not_required_returns_zero(self, make_plugin):
        plugin = make_plugin()
        plugin.find_owned = lambda **_: []
        assert plugin.collect_from_running() == 0

    def test_one_container_failure_does_not_block_the_rest(self, make_plugin, tmp_path):
        plugin = make_plugin()
        plugin.find_owned = lambda **_: [_container("a"), _container("b")]
        good = _data_file(tmp_path / "good", {"/var/task/a.py": [1]})
        plugin.driver = MagicMock()

        def collect(backend, container, dest, cfg):
            if container.id == "a":
                raise RuntimeError("docker hiccup")
            return [good]

        plugin.driver.collect.side_effect = collect
        with pytest.warns(UserWarning, match="collect failed for container sam-a"):
            assert plugin.collect_from_running() == 1


class TestCollectAtEnd:
    def _session(self):
        session = MagicMock()
        session.testsfailed = 0
        return session

    def test_skips_containers_already_collected(self, make_plugin):
        plugin = make_plugin()
        plugin.collected_ids = {"a"}
        plugin.explicit_calls = 1
        plugin.find_owned = lambda **_: [_container("a", status="exited")]
        _stub_collect(plugin, {})
        plugin._collect_at_end(self._session())
        plugin.driver.collect.assert_not_called()

    def test_warns_when_nothing_found_and_no_explicit_call(self, make_plugin):
        plugin = make_plugin()
        plugin.find_owned = lambda **_: []
        with pytest.warns(UserWarning, match="No matching containers"):
            plugin._collect_at_end(self._session())

    def test_silent_after_an_explicit_call(self, make_plugin, recwarn):
        plugin = make_plugin()
        plugin.explicit_calls = 1
        plugin.find_owned = lambda **_: []
        plugin._collect_at_end(self._session())
        assert not [w for w in recwarn.list if "No matching" in str(w.message)]

    def test_required_fails_the_session_when_containers_yield_nothing(self, make_plugin):
        plugin = make_plugin(required=True)
        plugin.find_owned = lambda **_: [_container("a", status="exited")]
        _stub_collect(plugin, {})
        session = self._session()
        plugin._collect_at_end(session)
        assert session.testsfailed == 1
        assert "yielded no coverage data" in plugin.failure

    def test_required_raises_in_an_xdist_worker(self, make_plugin):
        # A worker's testsfailed never reaches the controller's exit code.
        plugin = make_plugin(required=True, is_worker=True)
        plugin.find_owned = lambda **_: [_container("a", status="exited")]
        _stub_collect(plugin, {})
        with pytest.raises(RuntimeError, match="yielded no coverage data"):
            plugin._collect_at_end(self._session())


class TestContainerEnv:
    def test_bootstrap_plus_worker_marker(self, make_plugin, plugin_config, monkeypatch):
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        plugin_config.worker_env = "MARK"
        plugin = make_plugin()
        assert plugin.container_env() == {
            "COVERAGE_PROCESS_START": "/var/task/.coveragerc",
            "MARK": "gw0",
        }


class TestPytestSession:
    """Real pytest + pytest-cov sessions with a fake docker backend."""

    CONFTEST = """
import pytest_cov_container.plugin as plugin_module
from coverage.data import CoverageData
from pytest_cov_container.models import ContainerInfo

EXTRACT = {extract!r}
STATUS = {status!r}


class FakeBackend:
    def find_containers(self, image_pattern=None, label=None, predicate=None):
        return [ContainerInfo(id="c1", name="fake", image="x", labels={{}}, status=STATUS)]

    def send_signal(self, container_id):
        return 0

    def extract_matching_files(self, container_id, source_dir, prefix, dest):
        if not EXTRACT:
            return []
        path = dest / "c1-.coverage.container.h.1.a"
        data = CoverageData(basename=str(path))
        data.add_lines({{EXTRACT: [1, 2]}})
        data.close()
        return [path]


plugin_module.DockerBackend = FakeBackend
"""

    def _project(self, pytester, *, extract: bool, host_import: bool = True, explicit: bool = False):
        pytester.makefile(
            ".toml",
            pyproject="""
[tool.pytest-cov-container]
image_pattern = "x*"

[tool.pytest-cov-container.python]
build_dir = "build"
wrapper = false
""",
        )
        (pytester.path / "build").mkdir()
        src = pytester.path / "src"
        src.mkdir()
        (src / "container_app.py").write_text("def f():\n    return 1\n")
        (src / "host_mod.py").write_text("X = 1\n")
        target = str(src / "container_app.py") if extract else ""
        status = "running" if explicit else "exited"
        pytester.makeconftest(self.CONFTEST.format(extract=target, status=status))
        if explicit:
            # Collected in a fixture teardown, as a sam-local fixture does: inside
            # pytest's per-test catch_warnings, which undoes any filter set there.
            pytester.makepyfile(
                test_x="import pytest\nimport pytest_cov_container\n\n"
                "@pytest.fixture\ndef app():\n    yield\n"
                "    assert pytest_cov_container.collect_container_coverage() == 1\n\n"
                "def test_x(app):\n    pass\n"
            )
            return
        if not host_import:
            # The tests only drive the container: the host measures nothing.
            pytester.makepyfile(test_x="def test_x():\n    pass\n")
            return
        pytester.makepyfile(
            test_x="import sys\nsys.path.insert(0, 'src')\n\ndef test_x():\n    import host_mod\n    assert host_mod.X == 1\n"
        )

    def test_container_data_lands_in_pytest_covs_report(self, pytester):
        self._project(pytester, extract=True)
        result = pytester.runpytest_subprocess("--cov=src", "--cov-report=term-missing")
        assert result.ret == 0
        result.stdout.re_match_lines([r".*container_app\.py\s+2\s+0\s+100%"])

    @pytest.mark.parametrize("explicit", [False, True], ids=["at-end", "fixture-teardown"])
    def test_container_data_silences_the_hosts_no_data_warning(self, pytester, explicit):
        # coverage checks only the host's own (empty) data at save time; the
        # container's data is the coverage this run exists for.
        self._project(pytester, extract=True, host_import=False, explicit=explicit)
        result = pytester.runpytest_subprocess("--cov=src", "--cov-report=term-missing")
        assert result.ret == 0
        result.stdout.re_match_lines([r".*container_app\.py\s+2\s+0\s+100%"])
        assert "No data was collected" not in result.stdout.str() + result.stderr.str()

    def test_no_data_warning_stays_when_containers_yield_nothing(self, pytester):
        self._project(pytester, extract=False, host_import=False)
        result = pytester.runpytest_subprocess("--cov=src", "--cov-report=term-missing")
        assert "No data was collected" in result.stdout.str() + result.stderr.str()

    def test_required_fails_the_run_when_no_data(self, pytester):
        self._project(pytester, extract=False)
        result = pytester.runpytest_subprocess("--cov=src", "--cov-container-required")
        assert result.ret == 1
        result.stdout.fnmatch_lines(["*yielded no coverage data*"])

    def test_inactive_without_cov(self, pytester):
        self._project(pytester, extract=True)
        result = pytester.runpytest_subprocess()
        assert result.ret == 0
        assert not (pytester.path / "build" / ".coveragerc").exists()


class TestPytestConfigure:
    def test_registers_no_cov_container_option(self, pytester):
        result = pytester.runpytest("--help")
        result.stdout.fnmatch_lines(["*--no-cov-container*"])


class TestEndToEndDefaultPath:
    """Exercise the full move-and-shim chain: inject() then `bash run.sh`.

    Validates that the rendered wrapper runs the user's _orig_run.sh, that
    coverage data is written, and that exit code propagates from the child.
    No Docker dependency.
    """

    def test_end_to_end_default_path_writes_coverage(self, tmp_path):
        build = tmp_path / "build"
        build.mkdir()

        # User's tiny app
        app = build / "app.py"
        app.write_text(
            "def main():\n    return 1 + 2\n\nif __name__ == '__main__':\n    main()\n"
        )

        # User's run.sh exec's their app. Quote paths — sys.executable may
        # contain spaces (e.g. hatch venvs under "Application Support").
        run_sh = build / "run.sh"
        run_sh.write_text(f'#!/bin/bash\nexec "{sys.executable}" "{app}"\n')
        run_sh.chmod(0o755)

        # site-packages with a `.pth` calling coverage.process_startup() —
        # the gate inject() enforces, AND the mechanism subprocess attach uses.
        sp = build / "site-packages"
        sp.mkdir()
        (sp / "coverage_subprocess.pth").write_text(
            "import coverage; coverage.process_startup()\n"
        )

        driver = PythonDriver()
        result = driver.inject(
            build,
            DriverConfig(build_dir=str(build), entrypoint=None, path_mapping={}),
        )
        assert len(result.files_written) == 4

        # Wrapper artifacts now self-locate via dirname($0); no /var/task
        # substitution needed. Just redirect the hardcoded /tmp data_file
        # so the test doesn't pollute /tmp under xdist.
        rc = build / ".coveragerc"
        rc.write_text(
            rc.read_text().replace(
                "/tmp/.coverage.container", str(build / ".coverage.container")
            )
        )
        # Re-set executable bit (write_text may have preserved it, but be safe)
        run_sh.chmod(run_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        # Execute the shim. coverage data file inside build_dir for isolation.
        # Inherit parent env (so `python` resolves to the venv interpreter w/
        # coverage installed) plus point COVERAGE_PROCESS_START at our rc and
        # PYTHONPATH at the site-packages whose .pth attaches subprocess cov.
        import os

        env = {
            **os.environ,
            "COVERAGE_PROCESS_START": str(build / ".coveragerc"),
            "PYTHONPATH": str(sp),
        }
        proc = subprocess.run(  # noqa: S603
            ["bash", str(run_sh)],
            env=env,
            capture_output=True,
            timeout=10,
        )
        assert proc.returncode == 0, (
            f"wrapper exit nonzero: rc={proc.returncode}\n"
            f"stdout={proc.stdout.decode()}\nstderr={proc.stderr.decode()}"
        )

        # At least one .coverage.container* file written
        cov_files = list(build.glob(".coverage.container*"))
        assert cov_files, (
            f"no coverage data files in {build}; ls: {list(build.iterdir())}"
        )
        # Non-trivial size
        assert any(f.stat().st_size > 0 for f in cov_files)
