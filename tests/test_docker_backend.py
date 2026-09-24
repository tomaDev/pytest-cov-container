import io
import tarfile

from pytest_cov_container.docker_backend import DockerBackend


class TestFindContainers:
    def test_finds_by_label(self, mock_docker_client, mock_docker_container):
        backend = DockerBackend(client=mock_docker_client)
        containers = backend.find_containers(label="pytest-cov-container")
        mock_docker_client.containers.list.assert_called_once_with(
            all=True, filters={"label": "pytest-cov-container"}, ignore_removed=True
        )
        assert len(containers) == 1
        assert containers[0].id == mock_docker_container.id

    def test_finds_by_image_pattern(self, mock_docker_client):
        backend = DockerBackend(client=mock_docker_client)
        containers = backend.find_containers(image_pattern="samcli/lambda*")
        assert len(containers) == 1

    def test_image_pattern_filters_non_matching(self, mock_docker_client):
        backend = DockerBackend(client=mock_docker_client)
        containers = backend.find_containers(image_pattern="nginx*")
        assert len(containers) == 0

    def test_returns_empty_when_no_match(self, mock_docker_client):
        mock_docker_client.containers.list.return_value = []
        backend = DockerBackend(client=mock_docker_client)
        containers = backend.find_containers(label="nonexistent")
        assert containers == []

    def test_matches_config_image_without_tags(
        self, mock_docker_client, mock_docker_container
    ):
        # Config.Image is what the container was created from; tags need an
        # image lookup and vanish when the image is re-tagged.
        mock_docker_container.image.tags = []
        backend = DockerBackend(client=mock_docker_client)
        assert len(backend.find_containers(image_pattern="samcli*")) == 1

    def test_no_image_reference_never_matches(
        self, mock_docker_client, mock_docker_container
    ):
        mock_docker_container.image.tags = []
        mock_docker_container.attrs = {"Config": {}}
        backend = DockerBackend(client=mock_docker_client)
        assert backend.find_containers(image_pattern="samcli*") == []

    def test_accepts_a_list_of_patterns(self, mock_docker_client):
        backend = DockerBackend(client=mock_docker_client)
        found = backend.find_containers(image_pattern=["nginx*", "samcli/*"])
        assert len(found) == 1

    def test_predicate_filters_on_inspect_attrs(
        self, mock_docker_client, mock_docker_container
    ):
        backend = DockerBackend(client=mock_docker_client)
        seen = []
        found = backend.find_containers(
            predicate=lambda attrs: seen.append(attrs) or False
        )
        assert found == []
        assert seen == [mock_docker_container.attrs]

    def test_client_is_lazy(self, monkeypatch):
        import docker

        def boom():
            raise AssertionError("docker.from_env called at construction")

        monkeypatch.setattr(docker, "from_env", boom)
        DockerBackend()


class TestSendSignal:
    def test_signals_the_pid_file_process(
        self, mock_docker_client, mock_docker_container
    ):
        mock_docker_container.exec_run.return_value = (0, b"signalled=1\n")
        backend = DockerBackend(client=mock_docker_client)
        assert backend.send_signal(mock_docker_container.id) == 1
        script = mock_docker_container.exec_run.call_args[0][0][-1]
        assert "/tmp/.cov_container.pid" in script
        assert "kill -USR1" in script

    def test_clears_the_old_sentinel_before_signalling(self):
        from pytest_cov_container.docker_backend import _SIGNAL_CMD

        script = _SIGNAL_CMD[-1]
        assert script.index("rm -f /tmp/.cov_container.done") < script.index("kill")

    def test_never_scans_proc_cmdline(self):
        # A /proc scan's own `sh -c` carries the search token, matches itself
        # and SIGUSR1 (default action: terminate) kills it before it reports.
        from pytest_cov_container.docker_backend import _SIGNAL_CMD

        assert "/proc" not in " ".join(_SIGNAL_CMD)

    def test_zero_when_no_coverage_process(
        self, mock_docker_client, mock_docker_container
    ):
        mock_docker_container.exec_run.return_value = (0, b"signalled=0\n")
        backend = DockerBackend(client=mock_docker_client)
        assert backend.send_signal(mock_docker_container.id) == 0

    def test_minus_one_and_warning_on_api_error(
        self, mock_docker_client, mock_docker_container
    ):
        import docker.errors
        import pytest

        mock_docker_container.exec_run.side_effect = docker.errors.APIError("down")
        backend = DockerBackend(client=mock_docker_client)
        with pytest.warns(UserWarning, match="Failed to send signal"):
            assert backend.send_signal(mock_docker_container.id) == -1

    def test_signal_script_runs_in_posix_sh(self, tmp_path):
        # Execute the real script against a live process in a local sh, with
        # the protocol paths pointed at tmp_path.
        import signal
        import subprocess
        import sys
        import time

        from pytest_cov_container.docker_backend import _SIGNAL_CMD

        pid_file, done_file = tmp_path / "pid", tmp_path / "done"
        done_file.write_text("")  # stale sentinel from a previous save
        target = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-c",
                "import signal, sys, time\n"
                "signal.signal(signal.SIGUSR1, lambda *a: sys.exit(7))\n"
                "time.sleep(10)\n",
            ]
        )
        try:
            time.sleep(0.3)
            pid_file.write_text(str(target.pid))
            script = (
                _SIGNAL_CMD[-1]
                .replace("/tmp/.cov_container.pid", str(pid_file))
                .replace("/tmp/.cov_container.done", str(done_file))
            )
            out = subprocess.run(  # noqa: S603
                ["sh", "-c", script], capture_output=True, text=True, timeout=5
            )
            assert out.stdout.strip() == "signalled=1"
            assert target.wait(timeout=5) == 7
            assert not done_file.exists()
        finally:
            if target.poll() is None:
                target.send_signal(signal.SIGKILL)
                target.wait()

        pid_file.unlink()
        out = subprocess.run(  # noqa: S603
            ["sh", "-c", script], capture_output=True, text=True, timeout=5
        )
        assert out.stdout.strip() == "signalled=0"


class TestWaitForDone:
    def test_true_once_sentinel_exists(
        self, mock_docker_client, mock_docker_container
    ):
        mock_docker_container.exec_run.side_effect = [(1, b""), (0, b"")]
        backend = DockerBackend(client=mock_docker_client)
        assert backend.wait_for_done(
            mock_docker_container.id, timeout=1.0, interval=0.01
        )
        cmd = mock_docker_container.exec_run.call_args[0][0]
        assert cmd == ["test", "-f", "/tmp/.cov_container.done"]

    def test_false_on_timeout(self, mock_docker_client, mock_docker_container):
        mock_docker_container.exec_run.return_value = (1, b"")
        backend = DockerBackend(client=mock_docker_client)
        assert not backend.wait_for_done(
            mock_docker_container.id, timeout=0.05, interval=0.01
        )


class TestExtractMatchingFiles:
    def _make_tar_bytes(self, files: dict[str, bytes]) -> bytes:
        """Create tar bytes with given filename->content mapping."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            for name, content in files.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        return buf.getvalue()

    def test_extracts_matching_files(
        self, mock_docker_client, mock_docker_container, tmp_path
    ):
        tar_data = self._make_tar_bytes(
            {
                "tmp/.coverage.container.host.123.abc": b"cov-data-1",
                "tmp/.coverage.container.host.456.def": b"cov-data-2",
                "tmp/other_file.txt": b"not coverage",
            }
        )
        mock_docker_container.get_archive.return_value = (iter([tar_data]), {})
        backend = DockerBackend(client=mock_docker_client)

        extracted = backend.extract_matching_files(
            mock_docker_container.id, "/tmp", ".coverage.container", tmp_path
        )

        assert len(extracted) == 2
        assert all(p.exists() for p in extracted)
        # Prefixed with the container id: two containers' same-named files
        # must not overwrite each other in a shared destination.
        assert (
            tmp_path / "abc123def456-.coverage.container.host.123.abc"
        ).read_bytes() == b"cov-data-1"

    def test_skips_sqlite_side_files(
        self, mock_docker_client, mock_docker_container, tmp_path
    ):
        tar_data = self._make_tar_bytes(
            {
                "tmp/.coverage.container.h.1.a": b"db",
                "tmp/.coverage.container.h.1.a-journal": b"j",
            }
        )
        mock_docker_container.get_archive.return_value = (iter([tar_data]), {})
        backend = DockerBackend(client=mock_docker_client)
        extracted = backend.extract_matching_files(
            mock_docker_container.id, "/tmp", ".coverage.container", tmp_path
        )
        assert [p.name for p in extracted] == ["abc123def456-.coverage.container.h.1.a"]

    def test_returns_empty_when_no_match(
        self, mock_docker_client, mock_docker_container, tmp_path
    ):
        tar_data = self._make_tar_bytes({"tmp/unrelated.txt": b"data"})
        mock_docker_container.get_archive.return_value = (iter([tar_data]), {})
        backend = DockerBackend(client=mock_docker_client)

        extracted = backend.extract_matching_files(
            mock_docker_container.id, "/tmp", ".coverage.container", tmp_path
        )
        assert extracted == []

    def _make_tar_with_traversal(self) -> bytes:
        """Tar with a member whose name claims to escape into the host."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name="../../etc/.coverage.container.evil")
            info.size = 4
            tar.addfile(info, io.BytesIO(b"PWND"))
            # Plus one benign entry so we can confirm benign extraction works.
            ok = tarfile.TarInfo(name="tmp/.coverage.container.ok")
            ok.size = 4
            tar.addfile(ok, io.BytesIO(b"data"))
        return buf.getvalue()

    def _make_tar_with_symlink(self) -> bytes:
        """Tar with a symlink member whose name matches the prefix."""
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            link = tarfile.TarInfo(name="tmp/.coverage.container.linky")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
        return buf.getvalue()

    def test_rejects_path_traversal_member(
        self, mock_docker_client, mock_docker_container, tmp_path
    ):
        # Regression: a malicious container image could plant a tar member
        # whose name escapes the destination directory. Even though current
        # logic writes by basename only, defense-in-depth via PEP 706
        # data_filter (or 3.11 hand-check) must reject these members.
        tar_data = self._make_tar_with_traversal()
        mock_docker_container.get_archive.return_value = (iter([tar_data]), {})
        backend = DockerBackend(client=mock_docker_client)

        extracted = backend.extract_matching_files(
            mock_docker_container.id, "/tmp", ".coverage.container", tmp_path
        )
        # Benign member extracts; traversal member is filtered out.
        assert len(extracted) == 1
        assert extracted[0].name == "abc123def456-.coverage.container.ok"
        # And no escape file under tmp_path's parent.
        assert not (tmp_path.parent / ".coverage.container.evil").exists()

    def test_rejects_symlink_member(
        self, mock_docker_client, mock_docker_container, tmp_path
    ):
        tar_data = self._make_tar_with_symlink()
        mock_docker_container.get_archive.return_value = (iter([tar_data]), {})
        backend = DockerBackend(client=mock_docker_client)

        extracted = backend.extract_matching_files(
            mock_docker_container.id, "/tmp", ".coverage.container", tmp_path
        )
        assert extracted == []
