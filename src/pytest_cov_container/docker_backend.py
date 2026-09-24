import re
import sys
from collections.abc import Callable, Mapping
from typing import Any

import docker
import docker.errors

from pytest_cov_container import ownership, protocol
from pytest_cov_container.models import ContainerInfo

_SIGNALLED_RE = re.compile(rb"signalled=(\d+)")

# Engines that forward host.docker.internal to the host's loopback.
_DESKTOP_ENGINES = ("Docker Desktop", "OrbStack")
_DESKTOP_ENDPOINT = ("127.0.0.1", "host.docker.internal")

# No pid file means no coverage process in the container.
_SIGNAL_CMD = [
    "sh",
    "-c",
    f'pid=$(cat {protocol.PID_FILE} 2>/dev/null) || {{ echo "signalled=0"; exit 0; }}; '
    'kill -USR1 "$pid" && echo "signalled=1" || echo "signalled=0"',
]


class DockerBackend:
    def __init__(self, client: docker.DockerClient | None = None):
        self._client_override = client
        self._lazy_client: docker.DockerClient | None = None

    @property
    def _client(self) -> docker.DockerClient:
        # Lazy: constructing the plugin must not need a running daemon, so a
        # session that never collects (or checks for docker itself) is not
        # broken by ``docker.from_env()`` at configure time.
        if self._client_override is not None:
            return self._client_override
        if self._lazy_client is None:
            self._lazy_client = docker.from_env()
        return self._lazy_client

    def find_containers(
        self,
        image_pattern: str | list[str] | None = None,
        label: str | None = None,
        predicate: Callable[[Mapping[str, Any]], bool] | None = None,
    ) -> list[ContainerInfo]:
        """Containers (any state) matching label, image pattern(s) and ``predicate``.

        ``predicate`` receives the container's ``docker inspect`` attrs; the
        ownership checks in ``pytest_cov_container.ownership`` plug in here.
        """
        filters: dict = {}
        if label:
            filters["label"] = label

        # ignore_removed: list() inspects each container after listing them, and
        # a container removed in between (a sibling xdist worker's teardown, an
        # auto-removed one-shot run) would otherwise raise NotFound.
        containers = self._client.containers.list(all=True, filters=filters, ignore_removed=True)

        patterns = [image_pattern] if isinstance(image_pattern, str) else image_pattern
        if patterns:
            containers = [
                c
                for c in containers
                if ownership.image_matches([self._config_image(c)], patterns)
                or ownership.image_matches(self._image_tags(c), patterns)
            ]
        if predicate is not None:
            containers = [c for c in containers if predicate(c.attrs)]

        return [self._to_info(c) for c in containers]

    def host_endpoint(self, network: str | None = None, *, platform: str = sys.platform) -> tuple[str, str]:
        """``(bind, advertise)`` for a host service the containers must reach.

        Docker Desktop (and OrbStack) forward ``host.docker.internal`` to the
        host's loopback, so a loopback bind is reachable and stays private; the
        same holds for any VM-based engine on a macOS or Windows host. A Linux
        engine routes containers to the host through the network's gateway
        (``docker0``, typically ``172.17.0.1``): bind and advertise that IP, so
        no ``--add-host`` is needed and nothing outside Docker can connect.
        ``network`` is the one the containers join (default: ``bridge``).
        """
        info = self._client.info()
        engine = str(info.get("OperatingSystem") or "")
        if any(name in engine for name in _DESKTOP_ENGINES) or not platform.startswith("linux"):
            return _DESKTOP_ENDPOINT
        ipam = (self._client.networks.get(network or "bridge").attrs.get("IPAM") or {}).get("Config") or []
        gateway = next((entry.get("Gateway") for entry in ipam if entry.get("Gateway")), None)
        if not gateway:
            msg = (
                f"docker network {network or 'bridge'!r} has no gateway to reach the host through; "
                "set sink_bind and sink_host in [tool.pytest-cov-container]"
            )
            raise RuntimeError(msg)
        return gateway, gateway

    def send_signal(self, container_id: str) -> bool:
        """Send SIGUSR1 (push your coverage) to the process named by the pid file.

        Returns False when the container has no coverage process (no pid file,
        or the pid is gone). Docker errors propagate: the caller may run this on
        a worker thread and warns on its own thread, where pytest captures it.
        """
        container = self._client.containers.get(container_id)
        _, output = container.exec_run(_SIGNAL_CMD)
        match = _SIGNALLED_RE.search(output or b"")
        return bool(match and match.group(1) == b"1")

    @staticmethod
    def _config_image(container) -> str:
        """The image reference the container was created from.

        Needs no extra API call and survives the image being re-tagged or
        deleted, unlike ``container.image.tags`` (an image lookup per call).
        """
        return (container.attrs.get("Config") or {}).get("Image") or ""

    @staticmethod
    def _image_tags(container) -> list[str]:
        try:
            return list(container.image.tags or [])
        except docker.errors.DockerException:
            return []

    @classmethod
    def _to_info(cls, container) -> ContainerInfo:
        return ContainerInfo(
            id=container.id,
            name=container.name,
            image=cls._config_image(container),
            labels=container.labels,
            status=container.status,
        )
