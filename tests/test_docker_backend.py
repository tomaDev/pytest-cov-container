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

    def test_handles_container_without_image_tags(self, mock_docker_client, mock_docker_container):
        mock_docker_container.image.tags = []
        backend = DockerBackend(client=mock_docker_client)
        containers = backend.find_containers(image_pattern="samcli*")
        assert len(containers) == 0


class TestSendSignal:
    def test_sends_sigusr1_via_proc_walk(self, mock_docker_client, mock_docker_container):
        backend = DockerBackend(client=mock_docker_client)
        backend.send_signal(mock_docker_container.id)
        mock_docker_container.exec_run.assert_called_once()
        cmd = mock_docker_container.exec_run.call_args[0][0]
        assert "kill -USR1" in " ".join(cmd)
        assert "_cov_wrapper" in " ".join(cmd)


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

    def test_extracts_matching_files(self, mock_docker_client, mock_docker_container, tmp_path):
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
        assert (tmp_path / ".coverage.container.host.123.abc").read_bytes() == b"cov-data-1"

    def test_returns_empty_when_no_match(self, mock_docker_client, mock_docker_container, tmp_path):
        tar_data = self._make_tar_bytes({"tmp/unrelated.txt": b"data"})
        mock_docker_container.get_archive.return_value = (iter([tar_data]), {})
        backend = DockerBackend(client=mock_docker_client)

        extracted = backend.extract_matching_files(
            mock_docker_container.id, "/tmp", ".coverage.container", tmp_path
        )
        assert extracted == []
