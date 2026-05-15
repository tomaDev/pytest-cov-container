import fnmatch
import io
import tarfile
import time
import warnings
from pathlib import Path

import docker
import docker.errors

from pytest_cov_container.models import ContainerInfo

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


_SIGNAL_CMD = [
    "sh",
    "-c",
    "n=0; for p in /proc/[0-9]*/cmdline; do "
    'pid=$(basename "$(dirname "$p")"); '
    'if grep -aq _cov_wrapper "$p" 2>/dev/null; then '
    'kill -USR1 "$pid" && n=$((n+1)); fi; done; '
    'echo "signalled=$n"',
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
                if any(
                    fnmatch.fnmatch(tag, image_pattern) for tag in (c.image.tags or [])
                )
            ]

        return [self._to_info(c) for c in containers]

    def send_signal(self, container_id: str) -> None:
        try:
            container = self._client.containers.get(container_id)
            exit_code, output = container.exec_run(_SIGNAL_CMD)
        except docker.errors.APIError as exc:
            warnings.warn(
                f"Failed to send signal to container {container_id[:12]}: {exc}",
                UserWarning,
                stacklevel=2,
            )
            return

        # Parse `signalled=N` line from the shell loop. N=0 means no wrapper
        # process was found inside the container — almost always means the
        # injection step ran against the wrong build_dir or the container
        # didn't actually start the wrapper. Surface loudly rather than
        # silently producing an empty coverage extract.
        signalled = 0
        if output:
            for line in output.decode("utf-8", errors="replace").splitlines():
                if line.startswith("signalled="):
                    try:
                        signalled = int(line.split("=", 1)[1])
                    except ValueError:
                        pass
                    break
        if signalled == 0:
            warnings.warn(
                f"Container {container_id[:12]}: no wrapper process found "
                f"(exit_code={exit_code}). Coverage extract will be empty.",
                UserWarning,
                stacklevel=2,
            )

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

    def file_signature(self, container_id: str, source_dir: str, prefix: str) -> str:
        """Return the mtime of the newest file in `source_dir` whose name
        starts with `prefix`, formatted as a string. Empty string if no
        matching file or on docker error. Used as a baseline by
        `wait_for_save` to detect new save activity."""
        cmd = [
            "sh",
            "-c",
            f"find {source_dir} -maxdepth 1 -name '{prefix}*' "
            f"-printf '%T@\\n' 2>/dev/null | sort -rn | head -1",
        ]
        try:
            container = self._client.containers.get(container_id)
            _, output = container.exec_run(cmd)
        except docker.errors.APIError:
            return ""
        if not output:
            return ""
        return output.decode("utf-8", errors="replace").strip()

    def wait_for_save(
        self,
        container_id: str,
        source_dir: str,
        prefix: str,
        baseline: str,
        timeout: float = 2.0,
        interval: float = 0.05,
    ) -> bool:
        """Poll until the file signature in `source_dir` matching `prefix`
        differs from `baseline`, OR `timeout` seconds elapse. Returns True
        if a change was observed. Replaces the legacy 1-second blanket
        sleep: real coverage.save() takes 1-20 ms, so this returns in
        ~50 ms on the common path and caps wasted time on the slow path."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.file_signature(container_id, source_dir, prefix) != baseline:
                return True
            time.sleep(interval)
        return False

    @staticmethod
    def _to_info(container) -> ContainerInfo:
        return ContainerInfo(
            id=container.id,
            name=container.name,
            image=container.image.tags[0] if container.image.tags else "",
            labels=container.labels,
            status=container.status,
        )
