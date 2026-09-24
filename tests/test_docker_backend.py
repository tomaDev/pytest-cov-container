import pytest

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
            msg = "docker.from_env called at construction"
            raise AssertionError(msg)

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

        pid_file = tmp_path / "pid"
        target = subprocess.Popen(
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
            script = _SIGNAL_CMD[-1].replace("/tmp/.cov_container.pid", str(pid_file))
            out = subprocess.run(
                ["sh", "-c", script], check=False, capture_output=True, text=True, timeout=5
            )
            assert out.stdout.strip() == "signalled=1"
            assert target.wait(timeout=5) == 7
        finally:
            if target.poll() is None:
                target.send_signal(signal.SIGKILL)
                target.wait()

        pid_file.unlink()
        out = subprocess.run(
            ["sh", "-c", script], check=False, capture_output=True, text=True, timeout=5
        )
        assert out.stdout.strip() == "signalled=0"


class TestHostEndpoint:
    @staticmethod
    def _backend(mock_docker_client, engine: str, gateway: str | None = "172.17.0.1"):
        mock_docker_client.info.return_value = {"OperatingSystem": engine}
        config = [{"Subnet": "172.17.0.0/16", "Gateway": gateway}] if gateway else [{"Subnet": "172.17.0.0/16"}]
        mock_docker_client.networks.get.return_value.attrs = {"IPAM": {"Config": config}}
        return DockerBackend(client=mock_docker_client)

    @pytest.mark.parametrize("engine", ["Docker Desktop", "OrbStack"])
    def test_desktop_engines_forward_to_loopback(self, mock_docker_client, engine):
        backend = self._backend(mock_docker_client, engine)
        assert backend.host_endpoint(platform="linux") == ("127.0.0.1", "host.docker.internal")
        mock_docker_client.networks.get.assert_not_called()

    def test_any_engine_on_a_macos_host_is_vm_based(self, mock_docker_client):
        # colima, podman machine, Rancher Desktop: the engine reports its VM's OS.
        backend = self._backend(mock_docker_client, "Ubuntu 24.04 LTS")
        assert backend.host_endpoint(platform="darwin") == ("127.0.0.1", "host.docker.internal")

    def test_linux_engine_uses_the_bridge_gateway(self, mock_docker_client):
        backend = self._backend(mock_docker_client, "Ubuntu 24.04 LTS")
        assert backend.host_endpoint(platform="linux") == ("172.17.0.1", "172.17.0.1")
        mock_docker_client.networks.get.assert_called_once_with("bridge")

    def test_linux_engine_with_a_custom_network(self, mock_docker_client):
        backend = self._backend(mock_docker_client, "Debian GNU/Linux 12", gateway="172.20.0.1")
        assert backend.host_endpoint("sam-net", platform="linux") == ("172.20.0.1", "172.20.0.1")
        mock_docker_client.networks.get.assert_called_once_with("sam-net")

    def test_a_network_without_a_gateway_asks_for_the_overrides(self, mock_docker_client):
        backend = self._backend(mock_docker_client, "Ubuntu 24.04 LTS", gateway=None)
        with pytest.raises(RuntimeError, match="set sink_bind and sink_host"):
            backend.host_endpoint(platform="linux")

