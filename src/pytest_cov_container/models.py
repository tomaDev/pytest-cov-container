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
    entrypoint: str
    path_mapping: dict[str, str]


@dataclass
class InjectionResult:
    files_written: list[Path]
    env_vars: dict[str, str] = field(default_factory=dict)


class DockerBackendProtocol(Protocol):
    def send_signal(self, container_id: str) -> None: ...

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

    def inject(self, target_dir: Path, config: DriverConfig) -> InjectionResult: ...

    def collect(
        self,
        docker_backend: DockerBackendProtocol,
        container: ContainerInfo,
        dest: Path,
        config: DriverConfig,
    ) -> Path: ...
