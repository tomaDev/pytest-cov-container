import fnmatch
import io
import tarfile
from pathlib import Path

import docker

from pytest_cov_container.models import ContainerInfo

_SIGNAL_CMD = [
    "sh",
    "-c",
    "for p in /proc/[0-9]*/cmdline; do "
    "pid=$(echo $p | cut -d/ -f3); "
    "if grep -q _cov_wrapper $p 2>/dev/null; "
    "then kill -USR1 $pid; break; fi; done",
]


class DockerBackend:
    def __init__(self, client: docker.DockerClient | None = None):
        self._client = client or docker.from_env()

    def find_containers(
        self,
        image_pattern: str | None = None,
        label: str | None = None,
    ) -> list[ContainerInfo]:
        filters: dict = {}
        if label:
            filters["label"] = label

        containers = self._client.containers.list(all=True, filters=filters)

        if image_pattern:
            containers = [
                c
                for c in containers
                if any(fnmatch.fnmatch(tag, image_pattern) for tag in (c.image.tags or []))
            ]

        return [self._to_info(c) for c in containers]

    def send_signal(self, container_id: str) -> None:
        container = self._client.containers.get(container_id)
        container.exec_run(_SIGNAL_CMD)

    def extract_matching_files(
        self,
        container_id: str,
        source_dir: str,
        prefix: str,
        dest: Path,
    ) -> list[Path]:
        container = self._client.containers.get(container_id)
        stream, _ = container.get_archive(source_dir)

        tar_bytes = b"".join(stream)
        extracted: list[Path] = []
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
            for member in tar.getmembers():
                if not member.isfile():
                    continue
                name = Path(member.name).name
                if not name.startswith(prefix):
                    continue
                member_file = tar.extractfile(member)
                if member_file:
                    target = dest / name
                    target.write_bytes(member_file.read())
                    extracted.append(target)
        return extracted

    def inspect(self, container_id: str) -> dict:
        container = self._client.containers.get(container_id)
        return container.attrs

    @staticmethod
    def _to_info(container) -> ContainerInfo:
        return ContainerInfo(
            id=container.id,
            name=container.name,
            image=container.image.tags[0] if container.image.tags else "",
            labels=container.labels,
            status=container.status,
        )
