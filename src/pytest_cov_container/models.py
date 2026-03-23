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


@runtime_checkable
class LanguageDriver(Protocol):
    name: str

    def inject(self, target_dir: Path, config: DriverConfig) -> InjectionResult: ...

    def collect(
        self,
        docker_backend: object,
        container: ContainerInfo,
        dest: Path,
        config: DriverConfig,
    ) -> Path: ...
