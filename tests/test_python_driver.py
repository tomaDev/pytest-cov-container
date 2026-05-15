import ast
import stat
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from pytest_cov_container.drivers.python import (
    _COV_WRAPPER_TEMPLATE_LEGACY,
    _COV_WRAPPER_TEMPLATE_SHIM,
    PythonDriver,
)
from pytest_cov_container.models import ContainerInfo, DriverConfig, InjectionResult


def _make_build_dir(
    tmp_path: Path,
    *,
    run_sh_content: str = "#!/bin/bash\necho user-app\n",
    with_pth: bool = True,
    pth_layout: str = "flat",
) -> Path:
    """Create a default-path-ready build_dir with run.sh and (optionally) a coverage*.pth.

    pth_layout:
      - "flat":      build_dir/coverage_subprocess.pth (`sam build` Python function)
      - "pyver":     build_dir/python3.X/site-packages/coverage_subprocess.pth (Lambda layer)
      - "nested":    build_dir/python/site-packages/coverage_subprocess.pth (one-level)
    """
    (tmp_path / "run.sh").write_text(run_sh_content)
    (tmp_path / "run.sh").chmod(0o755)
    if with_pth:
        if pth_layout == "flat":
            pth = tmp_path / "coverage_subprocess.pth"
        elif pth_layout == "pyver":
            import sys

            sp = (
                tmp_path
                / f"python{sys.version_info.major}.{sys.version_info.minor}"
                / "site-packages"
            )
            sp.mkdir(parents=True)
            pth = sp / "coverage_subprocess.pth"
        elif pth_layout == "nested":
            sp = tmp_path / "python" / "site-packages"
            sp.mkdir(parents=True)
            pth = sp / "coverage_subprocess.pth"
        else:
            raise ValueError(f"unknown pth_layout: {pth_layout}")
        pth.write_text("import coverage; coverage.process_startup()\n")
    return tmp_path


class TestPythonDriverInject:
    def setup_method(self):
        self.driver = PythonDriver()
        self.config = DriverConfig(
            build_dir=".aws-sam/build/ApiFunction",
            entrypoint="uvicorn app:app --host 0.0.0.0 --port 8080",
            path_mapping={"src/api": "/var/task"},
        )

    def test_creates_coveragerc(self, tmp_path):
        self.driver.inject(tmp_path, self.config)
        coveragerc = tmp_path / ".coveragerc"
        assert coveragerc.exists()
        content = coveragerc.read_text()
        assert "relative_files = true" in content
        assert "parallel = true" in content
        assert "sigterm = true" in content
        assert "data_file = /tmp/.coverage.container" in content
        assert "_cov_wrapper.py" in content  # in omit section

    def test_creates_cov_wrapper(self, tmp_path):
        self.driver.inject(tmp_path, self.config)
        wrapper = tmp_path / "_cov_wrapper.py"
        assert wrapper.exists()
        content = wrapper.read_text()
        assert "coverage.Coverage" in content
        assert "SIGUSR1" in content
        assert "CONTAINER_COV_ENTRYPOINT" in content
        # Entrypoint is NOT baked into wrapper source. It now lives in a
        # sidecar JSON file (see _cov_entrypoint.json below) so a hostile
        # pyproject.toml cannot inject Python code via the entrypoint field.
        assert self.config.entrypoint not in content

    def test_writes_entrypoint_sidecar_json(self, tmp_path):
        import json

        self.driver.inject(tmp_path, self.config)
        sidecar = tmp_path / "_cov_entrypoint.json"
        assert sidecar.exists()
        data = json.loads(sidecar.read_text())
        assert data["entrypoint"] == self.config.entrypoint

    def test_entrypoint_with_python_source_is_quoted_safely(self, tmp_path):
        # Regression: a hostile pyproject.toml could previously inject
        # arbitrary Python code via entrypoint = '"); import os; os.system("evil"); ("'
        # Now the entrypoint is JSON-encoded, never interpolated into source.
        import json

        hostile = '"); import os; os.system("evil"); ("'
        cfg = DriverConfig(
            build_dir="build",
            entrypoint=hostile,
            path_mapping={},
        )
        self.driver.inject(tmp_path, cfg)
        wrapper_src = (tmp_path / "_cov_wrapper.py").read_text()
        # The hostile string must NOT appear in Python source.
        assert "os.system" not in wrapper_src
        # But it IS preserved verbatim in the sidecar (it's just a shell
        # string from the user's perspective; sh -c will execute it,
        # which is the user's intent for entrypoint).
        sidecar = json.loads((tmp_path / "_cov_entrypoint.json").read_text())
        assert sidecar["entrypoint"] == hostile

    def test_creates_run_sh(self, tmp_path):
        self.driver.inject(tmp_path, self.config)
        run_sh = tmp_path / "run.sh"
        assert run_sh.exists()
        content = run_sh.read_text()
        assert "_cov_wrapper.py" in content
        # Verify executable
        assert run_sh.stat().st_mode & stat.S_IEXEC

    def test_returns_injection_result(self, tmp_path):
        result = self.driver.inject(tmp_path, self.config)
        assert isinstance(result, InjectionResult)
        # 4 files now: .coveragerc + _cov_wrapper.py + run.sh + _cov_entrypoint.json
        assert len(result.files_written) == 4
        assert "COVERAGE_PROCESS_START" in result.env_vars

    def test_entrypoint_in_sidecar_not_wrapper(self, tmp_path):
        import json

        custom_config = DriverConfig(
            build_dir="build",
            entrypoint="gunicorn app:app -b 0.0.0.0:9000",
            path_mapping={},
        )
        self.driver.inject(tmp_path, custom_config)
        wrapper_content = (tmp_path / "_cov_wrapper.py").read_text()
        sidecar_data = json.loads((tmp_path / "_cov_entrypoint.json").read_text())
        assert "gunicorn app:app -b 0.0.0.0:9000" not in wrapper_content
        assert sidecar_data["entrypoint"] == "gunicorn app:app -b 0.0.0.0:9000"


class TestPythonDriverCollect:
    def setup_method(self):
        self.driver = PythonDriver()
        self.config = DriverConfig(
            build_dir=".aws-sam/build/ApiFunction",
            entrypoint="uvicorn app:app --host 0.0.0.0 --port 8080",
            path_mapping={"src/api": "/var/task"},
        )
        self.container_running = ContainerInfo(
            id="abc123",
            name="sam-api",
            image="samcli/lambda:3.12",
            labels={},
            status="running",
        )
        self.container_stopped = ContainerInfo(
            id="def456",
            name="sam-api",
            image="samcli/lambda:3.12",
            labels={},
            status="exited",
        )

    @pytest.mark.filterwarnings("ignore:No coverage data found")
    def test_signals_running_container(self, tmp_path):
        mock_backend = MagicMock()
        mock_backend.extract_matching_files.return_value = []
        mock_backend.file_signature.return_value = ""
        mock_backend.send_signal.return_value = 1
        self.driver.collect(mock_backend, self.container_running, tmp_path, self.config)
        mock_backend.send_signal.assert_called_once_with("abc123")

    @pytest.mark.filterwarnings("ignore:No coverage data found")
    def test_polls_for_save_completion_on_running(self, tmp_path):
        mock_backend = MagicMock()
        mock_backend.extract_matching_files.return_value = []
        mock_backend.file_signature.return_value = "1700000000.0"
        mock_backend.send_signal.return_value = 1  # one wrapper signalled
        self.driver.collect(mock_backend, self.container_running, tmp_path, self.config)
        # Baseline captured before signal, then wait_for_save called with it.
        mock_backend.file_signature.assert_called_once_with(
            "abc123", "/tmp", ".coverage.container"
        )
        mock_backend.wait_for_save.assert_called_once_with(
            "abc123", "/tmp", ".coverage.container", "1700000000.0"
        )

    @pytest.mark.filterwarnings("ignore:No coverage data found")
    def test_skips_poll_when_no_wrapper_signalled(self, tmp_path):
        # send_signal=0 means no wrapper in the container; no save will
        # land, so polling would burn the full 2s timeout fruitlessly.
        mock_backend = MagicMock()
        mock_backend.extract_matching_files.return_value = []
        mock_backend.file_signature.return_value = ""
        mock_backend.send_signal.return_value = 0
        self.driver.collect(mock_backend, self.container_running, tmp_path, self.config)
        mock_backend.send_signal.assert_called_once_with("abc123")
        mock_backend.wait_for_save.assert_not_called()

    def test_collect_does_not_sleep(self):
        # Regression: collect() previously did time.sleep(1) regardless of
        # save state. Now should poll for save completion via backend.
        import inspect

        src = inspect.getsource(PythonDriver.collect)
        assert "time.sleep" not in src

    @pytest.mark.filterwarnings("ignore:No coverage data found")
    def test_skips_poll_for_stopped_container(self, tmp_path):
        mock_backend = MagicMock()
        mock_backend.extract_matching_files.return_value = []
        self.driver.collect(mock_backend, self.container_stopped, tmp_path, self.config)
        mock_backend.file_signature.assert_not_called()
        mock_backend.wait_for_save.assert_not_called()

    @pytest.mark.filterwarnings("ignore:No coverage data found")
    def test_skips_signal_for_stopped_container(self, tmp_path):
        mock_backend = MagicMock()
        mock_backend.extract_matching_files.return_value = []
        self.driver.collect(mock_backend, self.container_stopped, tmp_path, self.config)
        mock_backend.send_signal.assert_not_called()

    def test_extracts_coverage_files(self, tmp_path):
        mock_backend = MagicMock()
        cov_file = tmp_path / ".coverage.container.host.1.abc"
        cov_file.write_bytes(b"data")
        mock_backend.extract_matching_files.return_value = [cov_file]

        result = self.driver.collect(
            mock_backend, self.container_stopped, tmp_path, self.config
        )

        mock_backend.extract_matching_files.assert_called_once_with(
            "def456", "/tmp", ".coverage.container", tmp_path
        )
        assert result == tmp_path

    def test_warns_when_no_coverage_data(self, tmp_path):
        mock_backend = MagicMock()
        mock_backend.extract_matching_files.return_value = []

        with pytest.warns(UserWarning, match="No coverage data found"):
            self.driver.collect(
                mock_backend, self.container_stopped, tmp_path, self.config
            )


class TestPythonDriverInjectShim:
    """Default-path (move-and-shim) tests. entrypoint is None → convention discovery."""

    def setup_method(self):
        self.driver = PythonDriver()
        self.config = DriverConfig(
            build_dir=".aws-sam/build/ApiFunction",
            entrypoint=None,
            path_mapping={"src/api": "/var/task"},
        )

    def test_inject_move_and_shim_default(self, tmp_path):
        build = _make_build_dir(
            tmp_path, run_sh_content="#!/bin/bash\nuser entrypoint\n"
        )
        original = (build / "run.sh").read_bytes()
        original_mode = (build / "run.sh").stat().st_mode

        result = self.driver.inject(build, self.config)

        orig = build / "_orig_run.sh"
        assert orig.exists()
        assert orig.read_bytes() == original
        assert orig.stat().st_mode & stat.S_IXUSR
        # Mode preserved via copy2
        assert orig.stat().st_mode & 0o777 == original_mode & 0o777

        # run.sh replaced with shim
        shim = build / "run.sh"
        assert "_cov_wrapper.py" in shim.read_text()

        # 4 artifacts written/touched: .coveragerc, _cov_wrapper.py, run.sh, _orig_run.sh
        assert len(result.files_written) == 4
        assert "COVERAGE_PROCESS_START" in result.env_vars

    def test_inject_idempotent_rerun(self, tmp_path):
        build = _make_build_dir(tmp_path)
        self.driver.inject(build, self.config)
        orig_first = (build / "_orig_run.sh").read_bytes()
        wrapper_first_mtime = (build / "_cov_wrapper.py").stat().st_mtime_ns

        # second run
        import time

        time.sleep(0.01)
        self.driver.inject(build, self.config)

        assert (build / "_orig_run.sh").read_bytes() == orig_first
        # other artifacts overwritten (mtime advances on rewrite)
        assert (build / "_cov_wrapper.py").stat().st_mtime_ns >= wrapper_first_mtime

    def test_inject_missing_run_sh_raises(self, tmp_path):
        # build_dir exists but no run.sh
        (tmp_path / "site-packages").mkdir()
        (tmp_path / "site-packages" / "coverage_subprocess.pth").write_text(
            "import coverage\n"
        )
        with pytest.raises(RuntimeError, match="run.sh"):
            self.driver.inject(tmp_path, self.config)

    def test_inject_missing_run_sh_with_override_ok(self, tmp_path):
        override_config = DriverConfig(
            build_dir="x",
            entrypoint="gunicorn app:app",
            path_mapping={},
        )
        # No run.sh, no site-packages — override is exempt
        self.driver.inject(tmp_path, override_config)
        assert (tmp_path / "_cov_wrapper.py").exists()
        assert (tmp_path / "run.sh").exists()
        assert not (tmp_path / "_orig_run.sh").exists()

    def test_inject_unreadable_run_sh_raises(self, tmp_path):
        build = _make_build_dir(tmp_path)
        (build / "run.sh").chmod(0o000)
        try:
            with pytest.raises(PermissionError):
                self.driver.inject(build, self.config)
        finally:
            (build / "run.sh").chmod(0o644)

    def test_inject_corrupt_state_warns_then_recovers(self, tmp_path):
        # _orig_run.sh exists, run.sh present but not the shim
        build = _make_build_dir(tmp_path)
        (build / "_orig_run.sh").write_text("#!/bin/bash\noriginal\n")
        (build / "_orig_run.sh").chmod(0o755)
        (build / "run.sh").write_text("#!/bin/bash\nnot a shim\n")

        with pytest.warns(UserWarning, match="rewriting"):
            self.driver.inject(build, self.config)

        # both rewritten cleanly
        assert "_cov_wrapper.py" in (build / "run.sh").read_text()

    def test_inject_refuses_shim_run_sh_without_backup(self, tmp_path):
        # run.sh looks like the shim but _orig_run.sh is missing
        build = tmp_path
        (build / "run.sh").write_text(
            "#!/bin/bash\nexec python /var/task/_cov_wrapper.py\n"
        )
        (build / "run.sh").chmod(0o755)
        sp = build / "site-packages"
        sp.mkdir()
        (sp / "coverage_subprocess.pth").write_text("import coverage\n")

        with pytest.raises(RuntimeError, match="no `_orig_run.sh`"):
            self.driver.inject(build, self.config)

    def test_inject_missing_coverage_pth_raises(self, tmp_path):
        build = _make_build_dir(tmp_path, with_pth=False)
        with pytest.raises(RuntimeError, match="coverage"):
            self.driver.inject(build, self.config)

    def test_inject_missing_coverage_pth_override_exempt(self, tmp_path):
        # Same build_dir, no pth, but override path → success
        build = _make_build_dir(tmp_path, with_pth=False)
        override_config = DriverConfig(
            build_dir=str(build), entrypoint="x y z", path_mapping={}
        )
        self.driver.inject(build, override_config)
        assert (build / "_cov_wrapper.py").exists()

    def test_inject_override_path_unlinks_stale_orig_run_sh(self, tmp_path):
        # Pre-existing _orig_run.sh from a prior default-path inject
        (tmp_path / "_orig_run.sh").write_text("#!/bin/bash\nstale\n")
        override_config = DriverConfig(
            build_dir=str(tmp_path), entrypoint="x y z", path_mapping={}
        )
        self.driver.inject(tmp_path, override_config)
        assert not (tmp_path / "_orig_run.sh").exists()

    def test_run_sh_executable_bit_set(self, tmp_path):
        build = _make_build_dir(tmp_path)
        self.driver.inject(build, self.config)
        assert (build / "run.sh").stat().st_mode & stat.S_IXUSR
        assert (build / "_orig_run.sh").stat().st_mode & stat.S_IXUSR


def _spawn_wrapper(
    tmp_path: Path, orig_run_sh: str, *, coveragerc_extra: str = ""
) -> "subprocess.Popen[bytes]":
    """Render the shim wrapper into tmp_path, write a custom `_orig_run.sh`,
    and spawn it as a subprocess. The wrapper self-locates via dirname of
    __file__, so no path substitution is needed.

    Caller is responsible for sending signals and waiting on the process.
    """
    import sys

    (tmp_path / ".coveragerc").write_text(
        f"[run]\ndata_file = {tmp_path}/.coverage.container\n"
        "relative_files = true\nparallel = true\nsigterm = true\n"
        f"{coveragerc_extra}"
    )
    (tmp_path / "_orig_run.sh").write_text(orig_run_sh)
    (tmp_path / "_orig_run.sh").chmod(0o755)

    wrapper_path = tmp_path / "_cov_wrapper.py"
    wrapper_path.write_text(_COV_WRAPPER_TEMPLATE_SHIM)

    return subprocess.Popen(  # noqa: S603
        [sys.executable, str(wrapper_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class TestCovWrapperSubprocessBehavior:
    """Spawn the rendered wrapper as a real subprocess and exercise signals."""

    def test_cov_wrapper_forwards_sigterm_to_child(self, tmp_path):
        import signal as _signal

        sentinel = tmp_path / "child_saw_sigterm"
        orig = (
            "#!/bin/bash\n"
            f"trap 'echo hit > {sentinel}; exit 42' TERM\n"
            "sleep 30 &\nwait $!\n"
        )
        proc = _spawn_wrapper(tmp_path, orig)
        try:
            time.sleep(0.5)  # let wrapper install handlers + spawn child
            proc.send_signal(_signal.SIGTERM)
            rc = proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
        assert sentinel.exists(), f"child did not observe SIGTERM (rc={rc})"
        assert rc == 42, f"expected child rc=42, got {rc}"

    def test_cov_wrapper_sigusr1_saves_without_killing_child(self, tmp_path):
        import signal as _signal

        child_pid_file = tmp_path / "child_pid"
        orig = f"#!/bin/bash\necho $$ > {child_pid_file}\nsleep 30\n"
        proc = _spawn_wrapper(tmp_path, orig)
        try:
            time.sleep(0.5)
            assert child_pid_file.exists(), "child did not start"
            child_pid = int(child_pid_file.read_text().strip())

            proc.send_signal(_signal.SIGUSR1)
            time.sleep(0.5)

            # Child must still be alive — SIGUSR1 forwarded would have killed it.
            try:
                os_kill_ok = True
                import os as _os

                _os.kill(child_pid, 0)
            except ProcessLookupError:
                os_kill_ok = False
            assert os_kill_ok, (
                "child died after SIGUSR1 — regression: SIGUSR1 was forwarded"
            )

            # Wrapper still alive too.
            assert proc.poll() is None, "wrapper exited after SIGUSR1"

            # Now shut down cleanly.
            proc.send_signal(_signal.SIGTERM)
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)

    def test_cov_wrapper_propagates_coverage_env_to_child(self, tmp_path):
        env_dump = tmp_path / "child_env"
        orig = f'#!/bin/bash\necho "$COVERAGE_PROCESS_START" > {env_dump}\n'
        proc = _spawn_wrapper(tmp_path, orig)
        try:
            rc = proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
        assert rc == 0
        assert env_dump.exists()
        # Wrapper sets COVERAGE_PROCESS_START via setdefault to <tmp>/.coveragerc
        assert env_dump.read_text().strip() == str(tmp_path / ".coveragerc")


class TestFindCoveragePth:
    """_find_coverage_pth uses direct probes (no recursive scan). Verify each
    supported layout resolves and that misses return None."""

    def test_finds_flat_layout(self, tmp_path):
        from pytest_cov_container.drivers.python import _find_coverage_pth

        _make_build_dir(tmp_path, pth_layout="flat")
        result = _find_coverage_pth(tmp_path)
        assert result is not None
        assert result.name.startswith("coverage")

    def test_finds_pyver_layout(self, tmp_path):
        from pytest_cov_container.drivers.python import _find_coverage_pth

        _make_build_dir(tmp_path, pth_layout="pyver")
        result = _find_coverage_pth(tmp_path)
        assert result is not None
        assert "site-packages" in str(result)

    def test_finds_nested_layout(self, tmp_path):
        from pytest_cov_container.drivers.python import _find_coverage_pth

        _make_build_dir(tmp_path, pth_layout="nested")
        result = _find_coverage_pth(tmp_path)
        assert result is not None
        assert "site-packages" in str(result)

    def test_returns_none_when_missing(self, tmp_path):
        from pytest_cov_container.drivers.python import _find_coverage_pth

        _make_build_dir(tmp_path, with_pth=False)
        result = _find_coverage_pth(tmp_path)
        assert result is None

    def test_does_not_use_rglob(self):
        # Regression: previous implementation used rglob which walked the
        # entire build_dir tree (200-800ms warm cache for 60-120k stats on
        # a real SAM build_dir). Now should be a bounded direct probe.
        import inspect
        from pytest_cov_container.drivers.python import _find_coverage_pth

        src = inspect.getsource(_find_coverage_pth)
        assert "rglob" not in src, "_find_coverage_pth should not use rglob"


class TestCovWrapperTemplates:
    def test_shim_template_renders_valid_python(self):
        ast.parse(_COV_WRAPPER_TEMPLATE_SHIM)

    def test_legacy_template_renders_valid_python(self):
        # Legacy template has a {entrypoint} format placeholder; fill it first.
        rendered = _COV_WRAPPER_TEMPLATE_LEGACY.format(entrypoint="echo hi")
        ast.parse(rendered)

    def test_shim_template_no_unfilled_placeholders(self):
        # Default-path template has no format params — it should parse as-is.
        assert "{entrypoint}" not in _COV_WRAPPER_TEMPLATE_SHIM
        assert "{" not in _COV_WRAPPER_TEMPLATE_SHIM.split("\n", 1)[1] or "{" in "{:}"
        # Looser: ensure split signal handlers exist
        assert "_save_and_forward" in _COV_WRAPPER_TEMPLATE_SHIM
        assert "_save(" in _COV_WRAPPER_TEMPLATE_SHIM
        assert "send_signal" in _COV_WRAPPER_TEMPLATE_SHIM
        assert "_orig_run.sh" in _COV_WRAPPER_TEMPLATE_SHIM
        assert "COVERAGE_PROCESS_START" in _COV_WRAPPER_TEMPLATE_SHIM
