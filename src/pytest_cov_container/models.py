from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class ContainerInfo:
    id: str
    name: str
    image: str
    labels: dict[str, str]
    status: str


@dataclass
class DriverConfig:
    build_dir: str
    entrypoint: str | None = None
    path_mapping: dict[str, str] = field(default_factory=dict)
    # False: the application starts coverage itself and follows the save
    # protocol (``pytest_cov_container.protocol``); the driver writes only the
    # ``.coveragerc`` and leaves ``run.sh`` alone.
    wrapper: bool = True
    # Host directory (relative to rootdir) whose ``*.py`` files are the only
    # ones measured in the container, rendered under ``container_root``.
    # ``None`` measures every ``*.py`` the container imports.
    source_dir: str | None = None
    container_root: str = "/var/task"
    # ``None``: inherit the host's coverage ``branch`` setting. The two must
    # match, or combining fails with "Can't combine branch coverage data with
    # statement data".
    branch: bool | None = None
    relative_files: bool = True


@dataclass
class InjectionResult:
    files_written: list[Path]
    env_vars: dict[str, str] = field(default_factory=dict)


class DockerBackendProtocol(Protocol):
    def send_signal(self, container_id: str) -> int: ...

    def wait_for_done(
        self,
        container_id: str,
        timeout: float = ...,
        interval: float = ...,
    ) -> bool: ...

    def extract_matching_files(
        self,
        container_id: str,
        source_dir: str,
        prefix: str,
        dest: Path,
    ) -> list[Path]: ...


@runtime_checkable
class LanguageDriver(Protocol):
    name: str

    def container_env(self, config: DriverConfig) -> dict[str, str]: ...

    def inject(
        self,
        target_dir: Path,
        config: DriverConfig,
        *,
        rootpath: Path | None = None,
        branch: bool = False,
    ) -> InjectionResult: ...

    def collect(
        self,
        docker_backend: DockerBackendProtocol,
        container: ContainerInfo,
        dest: Path,
        config: DriverConfig,
    ) -> list[Path]: ...
