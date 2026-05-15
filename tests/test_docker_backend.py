import io
import tarfile

from pytest_cov_container.docker_backend import DockerBackend


class TestFindContainers:
    def test_finds_by_label(self, mock_docker_client, mock_docker_container):
        backend = DockerBackend(client=mock_docker_client)
        containers = backend.find_containers(label="pytest-cov-container")
        mock_docker_client.containers.list.assert_called_once_with(
            all=True, filters={"label": "pytest-cov-container"}
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

    def test_handles_container_without_image_tags(
        self, mock_docker_client, mock_docker_container
    ):
        mock_docker_container.image.tags = []
        backend = DockerBackend(client=mock_docker_client)
        containers = backend.find_containers(image_pattern="samcli*")
        assert len(containers) == 0


class TestSendSignal:
    def test_sends_sigusr1_via_proc_walk(
        self, mock_docker_client, mock_docker_container
    ):
        mock_docker_container.exec_run.return_value = (0, b"signalled=1\n")
        backend = DockerBackend(client=mock_docker_client)
        backend.send_signal(mock_docker_container.id)
        mock_docker_container.exec_run.assert_called_once()
        cmd = mock_docker_container.exec_run.call_args[0][0]
        assert "kill -USR1" in " ".join(cmd)
        assert "_cov_wrapper" in " ".join(cmd)

    def test_signal_cmd_does_not_break_after_first_match(self):
        # Regression: previously the shell script used `break` after the
        # first match, missing additional wrapper PIDs in multi-process
        # setups. Signal command should iterate over all matches.
        from pytest_cov_container.docker_backend import _SIGNAL_CMD

        joined = " ".join(_SIGNAL_CMD)
        assert "break" not in joined

    def test_signal_cmd_emits_signalled_count(self):
        from pytest_cov_container.docker_backend import _SIGNAL_CMD

        joined = " ".join(_SIGNAL_CMD)
        assert "signalled=" in joined

    def test_warns_when_zero_pids_signalled(
        self, mock_docker_client, mock_docker_container
    ):
        import pytest

        # No wrapper found inside container — signalled=0 in stdout.
        mock_docker_container.exec_run.return_value = (0, b"signalled=0\n")
        backend = DockerBackend(client=mock_docker_client)
        with pytest.warns(UserWarning, match="no wrapper process"):
            backend.send_signal(mock_docker_container.id)

    def test_no_warning_when_pids_signalled(
        self, mock_docker_client, mock_docker_container, recwarn
    ):
        mock_docker_container.exec_run.return_value = (0, b"signalled=2\n")
        backend = DockerBackend(client=mock_docker_client)
        backend.send_signal(mock_docker_container.id)
        wrapper_warnings = [
            w for w in recwarn.list if "wrapper" in str(w.message).lower()
        ]
        assert wrapper_warnings == []


class TestFileSignature:
    def test_returns_mtime_of_newest_match(
        self, mock_docker_client, mock_docker_container
    ):
        mock_docker_container.exec_run.return_value = (0, b"1700000123.456\n")
        backend = DockerBackend(client=mock_docker_client)
        sig = backend.file_signature(
            mock_docker_container.id, "/tmp", ".coverage.container"
        )
        assert sig == "1700000123.456"

    def test_returns_empty_when_no_matches(
        self, mock_docker_client, mock_docker_container
    ):
        mock_docker_container.exec_run.return_value = (0, b"")
        backend = DockerBackend(client=mock_docker_client)
        sig = backend.file_signature(
            mock_docker_container.id, "/tmp", ".coverage.container"
        )
        assert sig == ""


class TestWaitForSave:
    def test_returns_true_when_signature_changes(
        self, mock_docker_client, mock_docker_container
    ):
        # First poll returns baseline (no change); second returns new sig.
        mock_docker_container.exec_run.side_effect = [
            (0, b"1700000000.0\n"),
            (0, b"1700000005.5\n"),
        ]
        backend = DockerBackend(client=mock_docker_client)
        ok = backend.wait_for_save(
            mock_docker_container.id,
            "/tmp",
            ".coverage.container",
            baseline="1700000000.0",
            timeout=1.0,
            interval=0.01,
        )
        assert ok is True

    def test_returns_false_on_timeout(self, mock_docker_client, mock_docker_container):
        # Signature never changes.
        mock_docker_container.exec_run.return_value = (0, b"1700000000.0\n")
        backend = DockerBackend(client=mock_docker_client)
        ok = backend.wait_for_save(
            mock_docker_container.id,
            "/tmp",
            ".coverage.container",
            baseline="1700000000.0",
            timeout=0.1,
            interval=0.01,
        )
        assert ok is False


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
        assert (
            tmp_path / ".coverage.container.host.123.abc"
        ).read_bytes() == b"cov-data-1"

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
        assert extracted[0].name == ".coverage.container.ok"
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
