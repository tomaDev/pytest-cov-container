import dataclasses
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import coverage
import docker.errors
import pytest
from coverage.data import CoverageData

from pytest_cov_container import config, protocol
from pytest_cov_container.models import ContainerInfo
from pytest_cov_container.plugin import ContainerCovPlugin


@pytest.fixture
def plugin_config(sam_project):
    (sam_project / "pyproject.toml").write_text(
        '[tool.pytest-cov-container]\nframework = "aws-sam"\nworker_env = "MARK"\n'
        'sink_host = "127.0.0.1"\nsink_bind = "127.0.0.1"\n'
    )
    loaded = config.load_config(sam_project / "pyproject.toml")
    assert loaded is not None
    return loaded


@pytest.fixture
def make_plugin(plugin_config, sam_project, monkeypatch):
    """A plugin with a mocked docker backend; pytest-cov's data file is
    ``<sam_project>/.coverage`` (the outer run's own Coverage is hidden)."""
    monkeypatch.setattr(coverage.Coverage, "current", classmethod(lambda cls: None))
    monkeypatch.setenv("COVERAGE_FILE", str(sam_project / ".coverage"))
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    made = []

    def make(**kwargs):
        with patch("pytest_cov_container.plugin.DockerBackend"):
            plugin = ContainerCovPlugin(plugin_config, sam_project, **kwargs)
        made.append(plugin)
        return plugin

    yield make
    for plugin in made:
        if plugin.sink is not None:
            plugin.sink.stop()


def _session(root: Path, *, controller: bool = False):
    session = MagicMock()
    session.testsfailed = 0
    session.config.rootpath = root
    session.config.pluginmanager.hasplugin.side_effect = lambda name: controller and name == "dsession"
    return session


def _data(path: Path, lines: dict[str, list[int]]) -> bytes:
    data = CoverageData(basename=str(path))
    data.add_lines(lines)
    data.write()
    return path.read_bytes()


def _container(cid="abc123def4560000", status="running"):
    return ContainerInfo(id=cid, name=f"sam-{cid[:4]}", image="x", labels={}, status=status)


class TestSessionStart:
    def test_injects_every_target_and_starts_the_sink(self, make_plugin, sam_project):
        plugin = make_plugin()
        plugin.pytest_sessionstart(_session(sam_project))
        for function in ("ApiFunction", "Worker"):
            assert (sam_project / ".aws-sam/build" / function / protocol.PTH_FILE).is_file()
        assert plugin.sink is not None

    def test_xdist_worker_serves_but_does_not_inject(self, make_plugin, sam_project):
        # The controller injected before spawning workers; a worker rewriting
        # the files could race a sibling's starting container.
        plugin = make_plugin(is_worker=True)
        plugin.pytest_sessionstart(_session(sam_project))
        assert not (sam_project / ".aws-sam/build/Worker" / protocol.PTH_FILE).exists()
        assert plugin.sink is not None

    def test_xdist_controller_injects_but_serves_nothing(self, make_plugin, sam_project):
        plugin = make_plugin()
        plugin.pytest_sessionstart(_session(sam_project, controller=True))
        assert (sam_project / ".aws-sam/build/Worker" / protocol.PTH_FILE).is_file()
        assert plugin.sink is None

    def test_missing_build_dir(self, make_plugin, sam_project):
        (sam_project / ".aws-sam/build/Worker").rmdir()
        with pytest.raises(FileNotFoundError, match="Run 'sam build'"):
            make_plugin().pytest_sessionstart(_session(sam_project))


class TestSinkAddress:
    def test_configured_values_skip_detection(self, make_plugin):
        plugin = make_plugin()
        assert plugin._sink_address() == ("127.0.0.1", "127.0.0.1")
        plugin.backend.host_endpoint.assert_not_called()

    def test_detected_when_unset(self, make_plugin, plugin_config):
        plugin_config.sink_bind = plugin_config.sink_host = None
        plugin_config.docker_network = "sam-net"
        plugin = make_plugin()
        plugin.backend.host_endpoint.return_value = ("172.17.0.1", "172.17.0.1")
        assert plugin._sink_address() == ("172.17.0.1", "172.17.0.1")
        plugin.backend.host_endpoint.assert_called_once_with("sam-net")

    def test_one_override_keeps_the_other_detected(self, make_plugin, plugin_config):
        plugin_config.sink_bind = None
        plugin = make_plugin()
        plugin.backend.host_endpoint.return_value = ("172.17.0.1", "172.17.0.1")
        assert plugin._sink_address() == ("172.17.0.1", "127.0.0.1")

    def test_no_engine_falls_back_to_docker_desktop(self, make_plugin, plugin_config):
        plugin_config.sink_bind = plugin_config.sink_host = None
        plugin = make_plugin()
        plugin.backend.host_endpoint.side_effect = docker.errors.DockerException("daemon down")
        with pytest.warns(UserWarning, match="assuming Docker Desktop"):
            assert plugin._sink_address() == ("127.0.0.1", "host.docker.internal")


class TestContainerEnv:
    def test_bootstrap_sink_and_marker(self, make_plugin, sam_project, monkeypatch):
        monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
        plugin = make_plugin(is_worker=True)
        plugin.pytest_sessionstart(_session(sam_project))
        assert plugin.sink is not None
        assert plugin.container_env() == {
            "COVERAGE_PROCESS_START": "/var/task/.coveragerc",
            "COV_CONTAINER_SINK": plugin.sink.url,
            "MARK": "gw0",
        }

    def test_marker_only_without_a_sink(self, make_plugin):
        assert make_plugin().container_env() == {"MARK": "main"}

    def test_targets_with_different_container_roots_are_refused(self, plugin_config, sam_project):
        # RCFILE_ENV is one value for every container this process starts.
        odd = dataclasses.replace(plugin_config.targets[0], name="Odd", container_root="/opt/app")
        cfg = dataclasses.replace(plugin_config, targets=(*plugin_config.targets, odd))
        with (
            patch("pytest_cov_container.plugin.DockerBackend"),
            pytest.raises(ValueError, match=r"different container_root values: \['/opt/app', '/var/task'\]"),
        ):
            ContainerCovPlugin(cfg, sam_project)


class TestOnPush:
    def test_maps_each_functions_paths_into_a_shard(self, make_plugin, sam_project, tmp_path):
        plugin = make_plugin()
        body = _data(
            tmp_path / "in",
            {"/var/task/handler.py": [1, 2], "/opt/python/shared/util.py": [1], "/var/lang/lib/x.py": [3]},
        )
        plugin._on_push("abc123def456-7", "Worker", body)
        shard = sam_project / ".coverage.container-main-abc123def456-7"
        assert plugin.shards == {"abc123def456-7": shard}
        data = CoverageData(basename=str(shard))
        data.read()
        assert data.measured_files() == {
            str(sam_project / "src/worker/handler.py"),
            str(sam_project / "src/shared/python/shared/util.py"),
            "/var/lang/lib/x.py",
        }
        assert not list(sam_project.glob(".cov-container-*.tmp"))

    def test_a_later_push_of_the_same_process_replaces_it(self, make_plugin, sam_project, tmp_path):
        plugin = make_plugin()
        plugin._on_push("abc123def456-7", "Worker", _data(tmp_path / "a", {"/var/task/handler.py": [1]}))
        plugin._on_push("abc123def456-7", "Worker", _data(tmp_path / "b", {"/var/task/handler.py": [1, 2]}))
        (shard,) = sam_project.glob(".coverage.container-*")
        data = CoverageData(basename=str(shard))
        data.read()
        assert data.lines(str(sam_project / "src/worker/handler.py")) == [1, 2]

    def test_unknown_function_is_refused(self, make_plugin, tmp_path):
        with pytest.raises(ValueError, match="unknown function 'Nope'"):
            make_plugin()._on_push("abc123def456-7", "Nope", _data(tmp_path / "a", {"/var/task/x.py": [1]}))

    def test_combines_with_host_data_via_coverage_combine(self, make_plugin, sam_project, tmp_path):
        # What pytest-cov's finish does: combine() picks up <data_file>.* files.
        _data(sam_project / ".coverage.host.1.abc", {"/host/mod.py": [1]})
        make_plugin()._on_push("abc123def456-7", "ApiFunction", _data(tmp_path / "a", {"/var/task/app.py": [1]}))
        cov = coverage.Coverage(data_file=str(sam_project / ".coverage"))
        cov.combine()
        assert cov.get_data().measured_files() == {"/host/mod.py", str(sam_project / "src/api/app.py")}


class TestFlush:
    def _plugin(self, make_plugin, sam_project, containers, *, pushes: bool, **kwargs):
        plugin = make_plugin(is_worker=True, **kwargs)
        plugin.pytest_sessionstart(_session(sam_project))
        plugin.find_owned = lambda **_: containers

        def send_signal(container_id):
            if pushes:
                plugin._on_push(f"{container_id[:12]}-1", "ApiFunction", _data(sam_project / "d", {"/var/task/app.py": [1]}))
                plugin.sink._record(f"{container_id[:12]}-1")
            return 1

        plugin.backend.send_signal.side_effect = send_signal
        return plugin

    def test_signals_running_containers_and_waits_for_their_push(self, make_plugin, sam_project):
        plugin = self._plugin(
            make_plugin, sam_project, [_container(), _container("0000000000aa0000", status="exited")], pushes=True
        )
        assert plugin.collect_from_running() == 1
        assert [c.args[0] for c in plugin.backend.send_signal.call_args_list] == ["abc123def4560000"]

    def test_warns_when_a_signalled_container_never_pushes(self, make_plugin, sam_project, monkeypatch):
        monkeypatch.setattr("pytest_cov_container.plugin._FLUSH_TIMEOUT_S", 0.2)
        plugin = self._plugin(make_plugin, sam_project, [_container()], pushes=False)
        with pytest.warns(UserWarning, match="did not push its coverage"):
            assert plugin.collect_from_running() == 0

    def test_signals_containers_concurrently(self, make_plugin, sam_project):
        # Each signal waits for the other: a sequential flush breaks the barrier.
        containers = [_container("aaaaaaaaaaaa0000"), _container("bbbbbbbbbbbb0000")]
        plugin = self._plugin(make_plugin, sam_project, containers, pushes=False)
        barrier = threading.Barrier(2, timeout=5)

        def send_signal(container_id):
            barrier.wait()
            ident = f"{container_id[:12]}-1"
            plugin._on_push(ident, "ApiFunction", _data(sam_project / ident, {"/var/task/app.py": [1]}))
            plugin.sink._record(ident)
            return True

        plugin.backend.send_signal.side_effect = send_signal
        assert plugin.collect_from_running() == 2

    def test_a_failing_signal_warns_and_the_rest_still_flush(self, make_plugin, sam_project):
        containers = [_container("aaaaaaaaaaaa0000"), _container()]
        plugin = self._plugin(make_plugin, sam_project, containers, pushes=True)
        pushing = plugin.backend.send_signal.side_effect

        def send_signal(container_id):
            if container_id.startswith("aaaa"):
                raise docker.errors.APIError("engine down")
            return pushing(container_id)

        plugin.backend.send_signal.side_effect = send_signal
        with pytest.warns(UserWarning, match=r"could not signal sam-aaaa \(aaaaaaaaaaaa\): engine down"):
            assert plugin.collect_from_running() == 1

    def test_required_raises_when_nothing_was_received(self, make_plugin, sam_project, monkeypatch):
        monkeypatch.setattr("pytest_cov_container.plugin._FLUSH_TIMEOUT_S", 0.1)
        plugin = self._plugin(make_plugin, sam_project, [], pushes=False, required=True)
        with pytest.raises(RuntimeError, match=r"no coverage received .*labelled sam\.cli\.container\.type=lambda"):
            plugin.collect_from_running()

    def test_no_sink_no_flush(self, make_plugin):
        assert make_plugin().collect_from_running() == 0


class TestRunTestLoop:
    @staticmethod
    def _run_loop(plugin, session):
        loop = plugin.pytest_runtestloop(session)
        next(loop)
        with pytest.raises(StopIteration):
            loop.send(True)

    def test_stops_the_sink_after_a_final_flush(self, make_plugin, sam_project):
        plugin = make_plugin(is_worker=True)
        plugin.pytest_sessionstart(_session(sam_project))
        plugin.find_owned = MagicMock(return_value=[])
        self._run_loop(plugin, _session(sam_project))
        plugin.find_owned.assert_called_once()
        with pytest.raises(urllib.error.URLError, match="refused"):
            urllib.request.urlopen(plugin.sink.url, timeout=1)

    def test_required_fails_a_plain_session_that_flushed_but_received_nothing(self, make_plugin, sam_project):
        plugin = make_plugin(required=True)
        plugin.pytest_sessionstart(_session(sam_project))
        plugin.explicit_calls = 1
        plugin.find_owned = MagicMock(return_value=[])
        session = _session(sam_project)
        self._run_loop(plugin, session)
        assert session.testsfailed == 1
        assert "no coverage received" in plugin.failure

    def test_required_raises_in_an_xdist_worker(self, make_plugin, sam_project):
        # A worker's testsfailed never reaches the controller's exit code.
        plugin = make_plugin(required=True, is_worker=True)
        plugin.pytest_sessionstart(_session(sam_project))
        plugin.explicit_calls = 1
        plugin.find_owned = MagicMock(return_value=[])
        with pytest.raises(RuntimeError, match="no coverage received"):
            self._run_loop(plugin, _session(sam_project))

    def test_required_ignores_a_process_that_never_touched_containers(self, make_plugin, sam_project):
        plugin = make_plugin(required=True, is_worker=True)
        plugin.pytest_sessionstart(_session(sam_project))
        plugin.find_owned = MagicMock(return_value=[])
        self._run_loop(plugin, _session(sam_project))
        assert plugin.failure is None


class TestPytestSession:
    """Real pytest + pytest-cov sessions; a test plays the container and pushes."""

    PUSH_TEST = """\
import urllib.request
from pathlib import Path

import pytest_cov_container
from coverage.data import CoverageData

PUSH = {push!r}


def test_container_pushes(tmp_path):
    env = pytest_cov_container.container_env(Path.cwd())
    if not PUSH:
        return
    data = CoverageData(basename=str(tmp_path / "d"))
    data.add_lines({{"/var/task/container_app.py": [1, 2]}})
    data.write()
    request = urllib.request.Request(
        env["COV_CONTAINER_SINK"],
        data=(tmp_path / "d").read_bytes(),
        method="POST",
        headers={{"X-Cov-Id": "abc123def456-9", "X-Cov-Function": "Fn"}},
    )
    urllib.request.urlopen(request, timeout=5).close()
"""

    CONFTEST = """\
import pytest
import pytest_cov_container
import pytest_cov_container.plugin as plugin_module


class FakeBackend:
    def find_containers(self, image_pattern=None, label=None, predicate=None):
        return []

    def send_signal(self, container_id):
        return 0


plugin_module.DockerBackend = FakeBackend


@pytest.fixture(autouse=True, scope="session")
def server():
    yield
    pytest_cov_container.collect_container_coverage()
"""

    def _project(self, pytester, *, push: bool):
        pytester.makefile(
            ".toml",
            pyproject=(
                '[tool.pytest-cov-container]\nframework = "aws-sam"\n'
                'sink_host = "127.0.0.1"\nsink_bind = "127.0.0.1"\n'
            ),
        )
        pytester.makefile(
            ".yaml",
            template=(
                "Resources:\n  Fn:\n    Type: AWS::Serverless::Function\n    Properties:\n"
                "      CodeUri: src/fn/\n      Runtime: python3.14\n      Handler: app.handler\n"
            ),
        )
        (pytester.path / ".aws-sam" / "build" / "Fn").mkdir(parents=True)
        src = pytester.path / "src" / "fn"
        src.mkdir(parents=True)
        (src / "container_app.py").write_text("def f():\n    return 1\n")
        pytester.makeconftest(self.CONFTEST)
        pytester.makepyfile(test_push=self.PUSH_TEST.format(push=push))

    def test_pushed_data_lands_in_pytest_covs_report(self, pytester):
        self._project(pytester, push=True)
        result = pytester.runpytest_subprocess("--cov=src", "--cov-report=term-missing")
        assert result.ret == 0
        result.stdout.re_match_lines([r".*container_app\.py\s+2\s+0\s+100%"])
        # The host measured nothing itself; the pushed data is the coverage.
        assert "No data was collected" not in result.stdout.str() + result.stderr.str()
        assert (pytester.path / ".aws-sam/build/Fn" / protocol.PTH_FILE).is_file()

    def test_required_fails_the_run_when_nothing_arrives(self, pytester):
        self._project(pytester, push=False)
        result = pytester.runpytest_subprocess("--cov=src", "--cov-container-required")
        assert result.ret != 0
        result.stdout.fnmatch_lines(["*no coverage received*"])

    def test_inactive_without_cov(self, pytester):
        self._project(pytester, push=False)
        result = pytester.runpytest_subprocess()
        assert result.ret == 0
        assert not (pytester.path / ".aws-sam/build/Fn" / protocol.PTH_FILE).exists()


class TestPytestConfigure:
    def test_registers_no_cov_container_option(self, pytester):
        result = pytester.runpytest("--help")
        result.stdout.fnmatch_lines(["*--no-cov-container*"])

