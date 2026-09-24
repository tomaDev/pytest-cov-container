import io
import re
import tarfile
import time
import warnings
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import docker
import docker.errors

from pytest_cov_container import ownership, protocol
from pytest_cov_container.models import ContainerInfo

_SIGNALLED_RE = re.compile(rb"signalled=(\d+)")

# SQLite side files that can sit next to a data file mid-write; never combine them.
_SQLITE_SIDE_SUFFIXES = ("-journal", "-wal", "-shm")

# PEP 706 data filter — present on 3.12+, polyfilled below for 3.11.
_tarfile_data_filter = getattr(tarfile, "data_filter", None)


def _is_safe_data_member(member: tarfile.TarInfo, dest: Path) -> bool:
    """Reject tar members that would escape `dest` or are not plain files.

    PEP 706 ``data`` filter on 3.12+; hand-rolled equivalent on 3.11. The
    code path here doesn't use ``extractall`` (it writes each file's
    basename to ``dest`` directly) so the data filter is defense-in-depth,
    not the sole barrier. The two practical hazards we block:

    1. Members whose name escapes the destination (`..` segments,
       absolute paths). The basename-rewrite in the caller already
       neutralises the escape on the host, but rejecting at filter time
       documents the intent and forward-protects any future caller that
       relaxes the basename trick (e.g. supporting nested directories).
    2. Non-regular members (symlinks, hardlinks, devices, fifos).
       ``isfile()`` already catches these; the filter call documents it.
    """
    if _tarfile_data_filter is not None:
        try:
            _tarfile_data_filter(member, str(dest))
        except (tarfile.FilterError, OSError):
            return False
        return member.isfile()
    # 3.11 polyfill: hand-check.
    if not member.isfile():
        return False
    name = member.name
    if name.startswith("/") or ".." in Path(name).parts:
        return False
    return True


# Clears the previous sentinel first, so a second collection never mistakes the
# first save's sentinel for its own. No pid file means no coverage process.
_SIGNAL_CMD = [
    "sh",
    "-c",
    f"rm -f {protocol.DONE_FILE}; "
    f'pid=$(cat {protocol.PID_FILE} 2>/dev/null) || {{ echo "signalled=0"; exit 0; }}; '
    'kill -USR1 "$pid" && echo "signalled=1" || echo "signalled=0"',
]
_DONE_CMD = ["test", "-f", protocol.DONE_FILE]


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
        containers = self._client.containers.list(
            all=True, filters=filters, ignore_removed=True
        )

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

    def send_signal(self, container_id: str) -> int:
        """Send SIGUSR1 to the coverage process named by the pid file.

        Returns 1 when signalled, 0 when the container has no coverage process
        (no pid file, or the pid is gone), -1 on a docker API error.
        """
        try:
            container = self._client.containers.get(container_id)
            _, output = container.exec_run(_SIGNAL_CMD)
        except docker.errors.APIError as exc:
            warnings.warn(
                f"Failed to send signal to container {container_id[:12]}: {exc}",
                UserWarning,
                stacklevel=2,
            )
            return -1
        match = _SIGNALLED_RE.search(output or b"")
        return int(match.group(1)) if match else 0

    def wait_for_done(
        self,
        container_id: str,
        timeout: float = 10.0,
        interval: float = 0.1,
    ) -> bool:
        """Poll for the save sentinel (``protocol.DONE_FILE``).

        True once it exists; False on timeout or docker error. A real save
        takes 1-20 ms; the timeout is headroom for a CPU-starved host.
        """
        try:
            container = self._client.containers.get(container_id)
        except docker.errors.APIError:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                exit_code, _ = container.exec_run(_DONE_CMD)
            except docker.errors.APIError:
                return False
            if exit_code == 0:
                return True
            time.sleep(interval)
        return False

    def extract_matching_files(
        self,
        container_id: str,
        source_dir: str,
        prefix: str,
        dest: Path,
    ) -> list[Path]:
        try:
            container = self._client.containers.get(container_id)
            stream, _ = container.get_archive(source_dir)
        except docker.errors.APIError as exc:
            warnings.warn(
                f"Failed to extract from container {container_id[:12]}: {exc}. Check that the container is accessible.",
                UserWarning,
                stacklevel=2,
            )
            return []

        tar_bytes = b"".join(stream)
        extracted: list[Path] = []
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
            for member in tar.getmembers():
                if not _is_safe_data_member(member, dest):
                    continue
                name = Path(member.name).name
                if not name.startswith(prefix) or name.endswith(_SQLITE_SIDE_SUFFIXES):
                    continue
                member_file = tar.extractfile(member)
                if member_file:
                    target = dest / f"{container_id[:12]}-{name}"
                    target.write_bytes(member_file.read())
                    extracted.append(target)
        return extracted

    def inspect(self, container_id: str) -> dict:
        container = self._client.containers.get(container_id)
        return container.attrs

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
