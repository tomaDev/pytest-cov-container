# pytest-cov-container Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a pytest plugin that collects code coverage from Python applications running inside Docker containers and combines it with local test coverage into a single report.

**Architecture:** Hook-based pytest plugin that injects coverage instrumentation into SAM build directories, discovers containers via Docker labels or image patterns, extracts coverage data after tests complete, and merges it with local coverage using `coverage combine`. Language drivers are pluggable via entry points (v1: Python only).

**Tech Stack:** Python 3.10+, pytest, pytest-cov, coverage.py 7.2+, Docker SDK for Python, hatch (project management)

**Design spec:** `docs/specs/2026-03-23-pytest-cov-container-design.md`

---

## File Structure

```
src/pytest_cov_container/
├── __about__.py              # version (exists)
├── __init__.py               # public API: collect_container_coverage()
├── config.py                 # load [tool.pytest-cov-container] from pyproject.toml
├── docker_backend.py         # DockerBackend: find, signal, extract containers
├── models.py                 # ContainerInfo, DriverConfig, InjectionResult, LanguageDriver protocol
├── plugin.py                 # pytest hooks: configure, sessionstart, sessionfinish
├── resolver.py               # SamBuildResolver: resolve build dir path
└── drivers/
    ├── __init__.py            # discover_drivers(), get_driver() via entry points
    └── python.py              # PythonDriver: inject + collect

tests/
├── __init__.py               # (exists)
├── conftest.py               # shared fixtures
├── test_config.py
├── test_docker_backend.py
├── test_driver_discovery.py
├── test_plugin.py
├── test_python_driver.py
└── test_resolver.py

pyproject.toml                # update: deps, entry points, hatch-test env
```

---

## Task 1: Project Setup

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/conftest.py`
- Create: `src/pytest_cov_container/drivers/__init__.py` (empty placeholder)

- [ ] **Step 1: Update pyproject.toml**

Replace the full content of `pyproject.toml` with:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "pytest-cov-container"
dynamic = ["version"]
description = "Pytest plugin to collect code coverage from applications running inside Docker containers"
readme = "README.md"
requires-python = ">=3.10"
license = "MIT"
keywords = ["pytest", "coverage", "docker", "containers", "testing"]
authors = [
  { name = "tomaDev", email = "genins21@gmail.com" },
]
classifiers = [
  "Development Status :: 3 - Alpha",
  "Framework :: Pytest",
  "Programming Language :: Python",
  "Programming Language :: Python :: 3.10",
  "Programming Language :: Python :: 3.11",
  "Programming Language :: Python :: 3.12",
  "Programming Language :: Python :: 3.13",
  "Programming Language :: Python :: Implementation :: CPython",
]
dependencies = [
  "pytest>=7.0",
  "pytest-cov>=4.0",
  "docker>=6.0",
  "coverage>=7.2",
  "tomli>=2.0; python_version < '3.11'",
]

[project.entry-points.pytest11]
cov_container = "pytest_cov_container.plugin"

[project.entry-points."pytest_cov_container.drivers"]
python = "pytest_cov_container.drivers.python:PythonDriver"

[project.urls]
Documentation = "https://github.com/tomaDev/pytest-cov-container#readme"
Issues = "https://github.com/tomaDev/pytest-cov-container/issues"
Source = "https://github.com/tomaDev/pytest-cov-container"

[tool.hatch.version]
path = "src/pytest_cov_container/__about__.py"

[tool.hatch.envs.hatch-test]
extra-dependencies = [
  "pytest-mock",
]

[tool.coverage.run]
source_pkgs = ["pytest_cov_container", "tests"]
branch = true
parallel = true
omit = [
  "src/pytest_cov_container/__about__.py",
]

[tool.coverage.paths]
pytest_cov_container = ["src/pytest_cov_container", "*/pytest-cov-container/src/pytest_cov_container"]
tests = ["tests", "*/pytest-cov-container/tests"]

[tool.coverage.report]
exclude_lines = [
  "no cov",
  "if __name__ == .__main__.:",
  "if TYPE_CHECKING:",
]
```

- [ ] **Step 2: Create tests/conftest.py**

```python
from pathlib import Path
from unittest.mock import MagicMock

import pytest


pytest_plugins = ["pytester"]


@pytest.fixture
def sample_pyproject(tmp_path):
    """Write a minimal pyproject.toml with plugin config and return its path."""
    content = """\
[tool.pytest-cov-container]
image_pattern = "samcli/lambda*"
label = "pytest-cov-container"

[tool.pytest-cov-container.path_mapping]
"src/api" = "/var/task"

[tool.pytest-cov-container.python]
build_dir = ".aws-sam/build/ApiFunction"
entrypoint = "uvicorn app:app --host 0.0.0.0 --port 8080"
"""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(content)
    return pyproject


@pytest.fixture
def mock_docker_container():
    """Return a MagicMock mimicking a docker Container object."""
    container = MagicMock()
    container.id = "abc123def456"
    container.name = "sam-local-api"
    container.image.tags = ["samcli/lambda-python:3.12"]
    container.labels = {"pytest-cov-container": "true"}
    container.status = "running"
    container.attrs = {"Config": {"Image": "samcli/lambda-python:3.12"}}
    return container


@pytest.fixture
def mock_docker_client(mock_docker_container):
    """Return a MagicMock mimicking docker.DockerClient."""
    client = MagicMock()
    client.containers.list.return_value = [mock_docker_container]
    client.containers.get.return_value = mock_docker_container
    return client
```

- [ ] **Step 3: Create empty driver package**

```python
# src/pytest_cov_container/drivers/__init__.py — placeholder, implemented in Task 8
```

- [ ] **Step 4: Verify setup**

Run: `hatch test -- --collect-only 2>&1 | head -5`
Expected: no import errors (may show "no tests collected")

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml tests/conftest.py src/pytest_cov_container/drivers/__init__.py
git commit -m "chore: configure project deps, entry points, and test fixtures"
```

---

## Task 2: Data Models

**Files:**
- Create: `src/pytest_cov_container/models.py`

No tests for this task — these are pure data containers tested implicitly by later tasks.

- [ ] **Step 1: Create models.py**

```python
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
```

- [ ] **Step 2: Verify import**

Run: `hatch run python -c "from pytest_cov_container.models import ContainerInfo, DriverConfig, InjectionResult, LanguageDriver; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add src/pytest_cov_container/models.py
git commit -m "feat: add core data models and LanguageDriver protocol"
```

---

## Task 3: Configuration Loading

**Files:**
- Create: `src/pytest_cov_container/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_config.py
from pathlib import Path

from pytest_cov_container import config


class TestLoadConfig:
    def test_returns_none_when_file_missing(self, tmp_path):
        result = config.load_config(tmp_path / "nonexistent.toml")
        assert result is None

    def test_returns_none_when_no_plugin_section(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'foo'\n")
        result = config.load_config(pyproject)
        assert result is None

    def test_loads_full_config(self, sample_pyproject):
        result = config.load_config(sample_pyproject)
        assert result is not None
        assert result.image_pattern == "samcli/lambda*"
        assert result.label == "pytest-cov-container"
        assert result.language == "python"
        assert result.enabled is True
        assert result.path_mapping == {"src/api": "/var/task"}

    def test_loads_driver_config(self, sample_pyproject):
        result = config.load_config(sample_pyproject)
        assert result.driver_config is not None
        assert result.driver_config.build_dir == ".aws-sam/build/ApiFunction"
        assert result.driver_config.entrypoint == "uvicorn app:app --host 0.0.0.0 --port 8080"
        assert result.driver_config.path_mapping == {"src/api": "/var/task"}

    def test_defaults_when_minimal_config(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[tool.pytest-cov-container]\nimage_pattern = "myapp*"\n')
        result = config.load_config(pyproject)
        assert result.language == "python"
        assert result.enabled is True
        assert result.path_mapping == {}
        assert result.driver_config.build_dir == ".aws-sam/build/ApiFunction"

    def test_disabled_config(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[tool.pytest-cov-container]\nenabled = false\n")
        result = config.load_config(pyproject)
        assert result.enabled is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `hatch test -- tests/test_config.py -v`
Expected: FAIL — `config` module has no `load_config`

- [ ] **Step 3: Implement config.py**

```python
# src/pytest_cov_container/config.py
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from pytest_cov_container.models import DriverConfig


@dataclass
class PluginConfig:
    image_pattern: Optional[str] = None
    label: Optional[str] = None
    language: str = "python"
    enabled: bool = True
    path_mapping: dict[str, str] = field(default_factory=dict)
    driver_config: Optional[DriverConfig] = None


def load_config(pyproject_path: Path) -> Optional[PluginConfig]:
    if not pyproject_path.exists():
        return None

    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)

    tool_config = data.get("tool", {}).get("pytest-cov-container")
    if tool_config is None:
        return None

    path_mapping = {str(k): str(v) for k, v in tool_config.get("path_mapping", {}).items()}

    plugin_config = PluginConfig(
        image_pattern=tool_config.get("image_pattern"),
        label=tool_config.get("label"),
        language=tool_config.get("language", "python"),
        enabled=tool_config.get("enabled", True),
        path_mapping=path_mapping,
    )

    driver_section = tool_config.get(plugin_config.language, {})
    plugin_config.driver_config = DriverConfig(
        build_dir=driver_section.get("build_dir", ".aws-sam/build/ApiFunction"),
        entrypoint=driver_section.get(
            "entrypoint", "uvicorn app:app --host 0.0.0.0 --port 8080"
        ),
        path_mapping=path_mapping,
    )

    return plugin_config
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `hatch test -- tests/test_config.py -v`
Expected: all 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/pytest_cov_container/config.py tests/test_config.py
git commit -m "feat: add configuration loading from pyproject.toml"
```

---

## Task 4: Build System Resolver

**Files:**
- Create: `src/pytest_cov_container/resolver.py`
- Create: `tests/test_resolver.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_resolver.py
from pathlib import Path

import pytest

from pytest_cov_container.resolver import SamBuildResolver


class TestSamBuildResolver:
    def test_resolves_relative_path(self, tmp_path):
        build_dir = tmp_path / ".aws-sam" / "build" / "ApiFunction"
        build_dir.mkdir(parents=True)
        resolver = SamBuildResolver()
        result = resolver.resolve_target_dir(".aws-sam/build/ApiFunction", tmp_path)
        assert result == build_dir

    def test_returns_path_even_if_missing(self, tmp_path):
        resolver = SamBuildResolver()
        result = resolver.resolve_target_dir(".aws-sam/build/ApiFunction", tmp_path)
        assert result == tmp_path / ".aws-sam" / "build" / "ApiFunction"
        assert not result.exists()

    def test_resolves_absolute_path(self, tmp_path):
        build_dir = tmp_path / "custom" / "build"
        build_dir.mkdir(parents=True)
        resolver = SamBuildResolver()
        result = resolver.resolve_target_dir(str(build_dir), tmp_path)
        assert result == build_dir
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `hatch test -- tests/test_resolver.py -v`
Expected: FAIL — `SamBuildResolver` not found

- [ ] **Step 3: Implement resolver.py**

```python
# src/pytest_cov_container/resolver.py
from pathlib import Path


class SamBuildResolver:
    def resolve_target_dir(self, build_dir: str, project_root: Path) -> Path:
        path = Path(build_dir)
        if path.is_absolute():
            return path
        return project_root / path
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `hatch test -- tests/test_resolver.py -v`
Expected: all 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/pytest_cov_container/resolver.py tests/test_resolver.py
git commit -m "feat: add SAM build directory resolver"
```

---

## Task 5: Docker Backend

**Files:**
- Create: `src/pytest_cov_container/docker_backend.py`
- Create: `tests/test_docker_backend.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_docker_backend.py
import io
import tarfile
from pathlib import Path
from unittest.mock import MagicMock

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `hatch test -- tests/test_docker_backend.py -v`
Expected: FAIL — `DockerBackend` not found

- [ ] **Step 3: Implement docker_backend.py**

```python
# src/pytest_cov_container/docker_backend.py
import fnmatch
import io
import tarfile
from pathlib import Path
from typing import Optional

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
    def __init__(self, client: Optional[docker.DockerClient] = None):
        self._client = client or docker.from_env()

    def find_containers(
        self,
        image_pattern: Optional[str] = None,
        label: Optional[str] = None,
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `hatch test -- tests/test_docker_backend.py -v`
Expected: all 7 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/pytest_cov_container/docker_backend.py tests/test_docker_backend.py
git commit -m "feat: add Docker backend for container discovery and file extraction"
```

---

## Task 6: Python Driver — Injection

**Files:**
- Create: `src/pytest_cov_container/drivers/python.py`
- Create: `tests/test_python_driver.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_python_driver.py
import stat
from pathlib import Path

from pytest_cov_container.drivers.python import PythonDriver
from pytest_cov_container.models import DriverConfig, InjectionResult


class TestPythonDriverInject:
    def setup_method(self):
        self.driver = PythonDriver()
        self.config = DriverConfig(
            build_dir=".aws-sam/build/ApiFunction",
            entrypoint="uvicorn app:app --host 0.0.0.0 --port 8080",
            path_mapping={"src/api": "/var/task"},
        )

    def test_creates_coveragerc(self, tmp_path):
        result = self.driver.inject(tmp_path, self.config)
        coveragerc = tmp_path / ".coveragerc"
        assert coveragerc.exists()
        content = coveragerc.read_text()
        assert "relative_files = true" in content
        assert "parallel = true" in content
        assert "sigterm = true" in content
        assert "data_file = /tmp/.coverage.container" in content
        assert "_cov_wrapper.py" in content  # in omit section

    def test_creates_cov_wrapper(self, tmp_path):
        result = self.driver.inject(tmp_path, self.config)
        wrapper = tmp_path / "_cov_wrapper.py"
        assert wrapper.exists()
        content = wrapper.read_text()
        assert "coverage.Coverage" in content
        assert "SIGUSR1" in content
        assert "CONTAINER_COV_ENTRYPOINT" in content
        assert self.config.entrypoint in content

    def test_creates_run_sh(self, tmp_path):
        result = self.driver.inject(tmp_path, self.config)
        run_sh = tmp_path / "run.sh"
        assert run_sh.exists()
        content = run_sh.read_text()
        assert "_cov_wrapper.py" in content
        # Verify executable
        assert run_sh.stat().st_mode & stat.S_IEXEC

    def test_returns_injection_result(self, tmp_path):
        result = self.driver.inject(tmp_path, self.config)
        assert isinstance(result, InjectionResult)
        assert len(result.files_written) == 3
        assert "COVERAGE_PROCESS_START" in result.env_vars

    def test_entrypoint_baked_into_wrapper(self, tmp_path):
        custom_config = DriverConfig(
            build_dir="build",
            entrypoint="gunicorn app:app -b 0.0.0.0:9000",
            path_mapping={},
        )
        self.driver.inject(tmp_path, custom_config)
        content = (tmp_path / "_cov_wrapper.py").read_text()
        assert "gunicorn app:app -b 0.0.0.0:9000" in content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `hatch test -- tests/test_python_driver.py::TestPythonDriverInject -v`
Expected: FAIL — `PythonDriver` not found

- [ ] **Step 3: Implement inject() in drivers/python.py**

```python
# src/pytest_cov_container/drivers/python.py
import stat
from pathlib import Path

from pytest_cov_container.models import ContainerInfo, DriverConfig, InjectionResult

_COVERAGERC_TEMPLATE = """\
[run]
data_file = /tmp/.coverage.container
relative_files = true
parallel = true
sigterm = true
include =
    *.py
omit =
    _cov_wrapper.py
"""

_COV_WRAPPER_TEMPLATE = """\
import os
import signal
import subprocess

import coverage

cov = coverage.Coverage(
    config_file=os.environ.get("COVERAGE_PROCESS_START", "/var/task/.coveragerc")
)
cov.start()


def _flush(*_):
    cov.save()


signal.signal(signal.SIGUSR1, _flush)

proc = subprocess.Popen(
    ["sh", "-c", os.environ.get("CONTAINER_COV_ENTRYPOINT", "{entrypoint}")]
)
proc.wait()
cov.stop()
cov.save()
"""

_RUN_SH_TEMPLATE = """\
#!/bin/bash
exec python /var/task/_cov_wrapper.py
"""


class PythonDriver:
    name: str = "python"

    def inject(self, target_dir: Path, config: DriverConfig) -> InjectionResult:
        files_written: list[Path] = []

        # .coveragerc
        coveragerc = target_dir / ".coveragerc"
        coveragerc.write_text(_COVERAGERC_TEMPLATE)
        files_written.append(coveragerc)

        # _cov_wrapper.py
        wrapper = target_dir / "_cov_wrapper.py"
        wrapper.write_text(_COV_WRAPPER_TEMPLATE.format(entrypoint=config.entrypoint))
        files_written.append(wrapper)

        # run.sh
        run_sh = target_dir / "run.sh"
        run_sh.write_text(_RUN_SH_TEMPLATE)
        run_sh.chmod(run_sh.stat().st_mode | stat.S_IEXEC)
        files_written.append(run_sh)

        return InjectionResult(
            files_written=files_written,
            env_vars={"COVERAGE_PROCESS_START": "/var/task/.coveragerc"},
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `hatch test -- tests/test_python_driver.py::TestPythonDriverInject -v`
Expected: all 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/pytest_cov_container/drivers/python.py tests/test_python_driver.py
git commit -m "feat: add Python driver injection (coveragerc, wrapper, run.sh)"
```

---

## Task 7: Python Driver — Collection

**Files:**
- Modify: `src/pytest_cov_container/drivers/python.py` (add `collect()`)
- Modify: `tests/test_python_driver.py` (add collection tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_python_driver.py`:

```python
from unittest.mock import MagicMock, patch

from pytest_cov_container.models import ContainerInfo


class TestPythonDriverCollect:
    def setup_method(self):
        self.driver = PythonDriver()
        self.config = DriverConfig(
            build_dir=".aws-sam/build/ApiFunction",
            entrypoint="uvicorn app:app --host 0.0.0.0 --port 8080",
            path_mapping={"src/api": "/var/task"},
        )
        self.container_running = ContainerInfo(
            id="abc123", name="sam-api", image="samcli/lambda:3.12",
            labels={}, status="running",
        )
        self.container_stopped = ContainerInfo(
            id="def456", name="sam-api", image="samcli/lambda:3.12",
            labels={}, status="exited",
        )

    def test_signals_running_container(self, tmp_path):
        mock_backend = MagicMock()
        mock_backend.extract_matching_files.return_value = []
        self.driver.collect(mock_backend, self.container_running, tmp_path, self.config)
        mock_backend.send_signal.assert_called_once_with("abc123")

    def test_skips_signal_for_stopped_container(self, tmp_path):
        mock_backend = MagicMock()
        mock_backend.extract_matching_files.return_value = []
        self.driver.collect(mock_backend, self.container_stopped, tmp_path, self.config)
        mock_backend.send_signal.assert_not_called()

    def test_extracts_coverage_files(self, tmp_path):
        mock_backend = MagicMock()
        cov_file = tmp_path / ".coverage.container.host.1.abc"
        cov_file.write_bytes(b"data")
        mock_backend.extract_matching_files.return_value = [cov_file]

        result = self.driver.collect(
            mock_backend, self.container_stopped, tmp_path, self.config
        )

        mock_backend.extract_matching_files.assert_called_once_with(
            "def456", "/tmp", ".coverage.container", tmp_path
        )
        assert result == tmp_path

    def test_warns_when_no_coverage_data(self, tmp_path):
        mock_backend = MagicMock()
        mock_backend.extract_matching_files.return_value = []

        with pytest.warns(UserWarning, match="No coverage data found"):
            self.driver.collect(
                mock_backend, self.container_stopped, tmp_path, self.config
            )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `hatch test -- tests/test_python_driver.py::TestPythonDriverCollect -v`
Expected: FAIL — `PythonDriver` has no `collect` method

- [ ] **Step 3: Add collect() to PythonDriver**

Append to the `PythonDriver` class in `src/pytest_cov_container/drivers/python.py`:

```python
    def collect(
        self,
        docker_backend: object,
        container: ContainerInfo,
        dest: Path,
        config: DriverConfig,
    ) -> Path:
        import time
        import warnings

        if container.status == "running":
            docker_backend.send_signal(container.id)
            time.sleep(1)

        extracted = docker_backend.extract_matching_files(
            container.id, "/tmp", ".coverage.container", dest
        )

        if not extracted:
            warnings.warn(
                f"No coverage data found in container {container.name} ({container.id[:12]})",
                UserWarning,
                stacklevel=2,
            )

        return dest
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `hatch test -- tests/test_python_driver.py -v`
Expected: all 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/pytest_cov_container/drivers/python.py tests/test_python_driver.py
git commit -m "feat: add Python driver collection (signal, extract, warn)"
```

---

## Task 8: Driver Discovery

**Files:**
- Modify: `src/pytest_cov_container/drivers/__init__.py`
- Create: `tests/test_driver_discovery.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_driver_discovery.py
import pytest

from pytest_cov_container import drivers
from pytest_cov_container.drivers.python import PythonDriver
from pytest_cov_container.models import LanguageDriver


class TestDiscoverDrivers:
    def test_discovers_python_driver(self):
        found = drivers.discover_drivers()
        assert "python" in found
        assert isinstance(found["python"], PythonDriver)

    def test_python_driver_satisfies_protocol(self):
        found = drivers.discover_drivers()
        assert isinstance(found["python"], LanguageDriver)


class TestGetDriver:
    def test_returns_python_driver(self):
        driver = drivers.get_driver("python")
        assert isinstance(driver, PythonDriver)

    def test_raises_for_unknown_driver(self):
        with pytest.raises(ValueError, match="Unknown language driver 'ruby'"):
            drivers.get_driver("ruby")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `hatch test -- tests/test_driver_discovery.py -v`
Expected: FAIL — `discover_drivers` not found

- [ ] **Step 3: Implement drivers/__init__.py**

```python
# src/pytest_cov_container/drivers/__init__.py
from importlib.metadata import entry_points

from pytest_cov_container.models import LanguageDriver


def discover_drivers() -> dict[str, LanguageDriver]:
    eps = entry_points(group="pytest_cov_container.drivers")
    drivers: dict[str, LanguageDriver] = {}
    for ep in eps:
        driver_class = ep.load()
        drivers[ep.name] = driver_class()
    return drivers


def get_driver(name: str) -> LanguageDriver:
    found = discover_drivers()
    if name not in found:
        available = ", ".join(sorted(found.keys()))
        msg = f"Unknown language driver '{name}'. Available: {available}"
        raise ValueError(msg)
    return found[name]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `hatch test -- tests/test_driver_discovery.py -v`
Expected: all 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/pytest_cov_container/drivers/__init__.py tests/test_driver_discovery.py
git commit -m "feat: add entry-point-based driver discovery"
```

---

## Task 9: Plugin Hooks

**Files:**
- Create: `src/pytest_cov_container/plugin.py`
- Create: `tests/test_plugin.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_plugin.py
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pytest_cov_container.config import PluginConfig
from pytest_cov_container.models import ContainerInfo, DriverConfig
from pytest_cov_container.plugin import ContainerCovPlugin


@pytest.fixture
def plugin_config():
    return PluginConfig(
        image_pattern="samcli/lambda*",
        label="pytest-cov-container",
        language="python",
        enabled=True,
        path_mapping={"src/api": "/var/task"},
        driver_config=DriverConfig(
            build_dir=".aws-sam/build/ApiFunction",
            entrypoint="uvicorn app:app --host 0.0.0.0 --port 8080",
            path_mapping={"src/api": "/var/task"},
        ),
    )


class TestContainerCovPluginSessionStart:
    @patch("pytest_cov_container.plugin.DockerBackend")
    def test_injects_when_build_dir_exists(self, mock_backend_cls, plugin_config, tmp_path):
        build_dir = tmp_path / ".aws-sam" / "build" / "ApiFunction"
        build_dir.mkdir(parents=True)
        plugin_config.driver_config.build_dir = str(build_dir)

        plugin = ContainerCovPlugin(plugin_config)
        mock_session = MagicMock()
        mock_session.config.rootpath = tmp_path

        plugin.pytest_sessionstart(mock_session)

        assert plugin.injection_result is not None
        assert (build_dir / ".coveragerc").exists()
        assert (build_dir / "_cov_wrapper.py").exists()
        assert (build_dir / "run.sh").exists()

    @patch("pytest_cov_container.plugin.DockerBackend")
    def test_raises_when_build_dir_missing(self, mock_backend_cls, plugin_config, tmp_path):
        plugin = ContainerCovPlugin(plugin_config)
        mock_session = MagicMock()
        mock_session.config.rootpath = tmp_path

        with pytest.raises(FileNotFoundError, match="does not exist"):
            plugin.pytest_sessionstart(mock_session)


class TestContainerCovPluginSessionFinish:
    @patch("pytest_cov_container.plugin.DockerBackend")
    @patch("subprocess.run")
    def test_warns_when_no_containers_found(self, mock_subprocess, mock_backend_cls, plugin_config):
        plugin = ContainerCovPlugin(plugin_config)
        plugin.backend.find_containers.return_value = []

        mock_session = MagicMock()
        mock_session.config.rootpath = Path("/project")
        with pytest.warns(UserWarning, match="No matching containers"):
            plugin.pytest_sessionfinish(mock_session, exitstatus=0)

    @patch("pytest_cov_container.plugin.DockerBackend")
    @patch("subprocess.run")
    def test_collects_and_combines(self, mock_subprocess, mock_backend_cls, plugin_config):
        plugin = ContainerCovPlugin(plugin_config)
        container = ContainerInfo(
            id="abc123", name="test", image="samcli/lambda:3.12",
            labels={}, status="exited",
        )
        plugin.backend.find_containers.return_value = [container]
        plugin.backend.extract_matching_files.return_value = []

        mock_session = MagicMock()
        mock_session.config.rootpath = Path("/project")

        with pytest.warns(UserWarning):
            plugin.pytest_sessionfinish(mock_session, exitstatus=0)

        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        cmd = call_args[0][0]
        assert cmd[0] == "coverage"
        assert cmd[1] == "combine"

    @patch("pytest_cov_container.plugin.DockerBackend")
    @patch("subprocess.run")
    def test_writes_paths_config(self, mock_subprocess, mock_backend_cls, plugin_config):
        plugin = ContainerCovPlugin(plugin_config)
        container = ContainerInfo(
            id="abc123", name="test", image="samcli/lambda:3.12",
            labels={}, status="exited",
        )
        plugin.backend.find_containers.return_value = [container]
        plugin.backend.extract_matching_files.return_value = []

        mock_session = MagicMock()
        mock_session.config.rootpath = Path("/project")

        with pytest.warns(UserWarning):
            plugin.pytest_sessionfinish(mock_session, exitstatus=0)

        rc_path = plugin.coverage_dir / ".coveragerc"
        assert rc_path.exists()
        content = rc_path.read_text()
        assert "src/api" in content
        assert "/var/task" in content


class TestPytestConfigure:
    def test_registers_no_cov_container_option(self, pytester):
        result = pytester.runpytest("--help")
        result.stdout.fnmatch_lines(["*--no-cov-container*"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `hatch test -- tests/test_plugin.py -v`
Expected: FAIL — `plugin` module or `ContainerCovPlugin` not found

- [ ] **Step 3: Implement plugin.py**

```python
# src/pytest_cov_container/plugin.py
import subprocess
import tempfile
import warnings
from pathlib import Path
from typing import Optional

import pytest

import pytest_cov_container
from pytest_cov_container import config as config_module
from pytest_cov_container import drivers
from pytest_cov_container.docker_backend import DockerBackend
from pytest_cov_container.resolver import SamBuildResolver


class ContainerCovPlugin:
    def __init__(self, plugin_config):
        self.config = plugin_config
        self.backend = DockerBackend()
        self.driver = drivers.get_driver(plugin_config.language)
        self.resolver = SamBuildResolver()
        self.injection_result = None
        self.coverage_dir = Path(tempfile.mkdtemp(prefix="cov_container_"))

    def pytest_sessionstart(self, session):
        target_dir = self.resolver.resolve_target_dir(
            self.config.driver_config.build_dir,
            session.config.rootpath,
        )
        if not target_dir.exists():
            msg = (
                f"Build directory {target_dir} does not exist. "
                f"Run 'sam build' before running tests."
            )
            raise FileNotFoundError(msg)

        self.injection_result = self.driver.inject(target_dir, self.config.driver_config)

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        containers = self.backend.find_containers(
            image_pattern=self.config.image_pattern,
            label=self.config.label,
        )

        if not containers:
            warnings.warn(
                "No matching containers found for coverage collection",
                UserWarning,
                stacklevel=2,
            )

        for container in containers:
            self.driver.collect(
                self.backend, container, self.coverage_dir, self.config.driver_config
            )

        self._combine_coverage(session.config.rootpath)

    def collect_from_running(self):
        containers = self.backend.find_containers(
            image_pattern=self.config.image_pattern,
            label=self.config.label,
        )
        running = [c for c in containers if c.status == "running"]
        for container in running:
            self.driver.collect(
                self.backend, container, self.coverage_dir, self.config.driver_config
            )

    def _combine_coverage(self, root: Path):
        rc_path = self.coverage_dir / ".coveragerc"
        lines = ["[paths]", "source ="]
        for host_path, container_path in self.config.path_mapping.items():
            lines.append(f"    {host_path}")
            lines.append(f"    {container_path}")
        rc_path.write_text("\n".join(lines) + "\n")

        result = subprocess.run(
            ["coverage", "combine", f"--rcfile={rc_path}", str(self.coverage_dir)],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            warnings.warn(
                f"coverage combine failed: {result.stderr}",
                UserWarning,
                stacklevel=2,
            )


def pytest_addoption(parser):
    group = parser.getgroup("cov-container")
    group.addoption(
        "--no-cov-container",
        action="store_true",
        default=False,
        help="Disable container coverage collection",
    )


@pytest.hookimpl(trylast=True)
def pytest_configure(config):
    if config.getoption("--no-cov-container", default=False):
        return

    if not config.pluginmanager.hasplugin("pytest_cov"):
        return

    cov_sources = config.getoption("--cov", default=[])
    if not cov_sources:
        return

    plugin_config = config_module.load_config(config.rootpath / "pyproject.toml")
    if plugin_config is None or not plugin_config.enabled:
        return

    plugin = ContainerCovPlugin(plugin_config)
    config.pluginmanager.register(plugin, "cov_container")
    pytest_cov_container._active_plugin = plugin
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `hatch test -- tests/test_plugin.py -v`
Expected: all 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/pytest_cov_container/plugin.py tests/test_plugin.py
git commit -m "feat: add pytest plugin hooks (configure, sessionstart, sessionfinish)"
```

---

## Task 10: Public API and Final Wiring

> **Deferred from v1:** The design spec defines `container_cov_build` and `container_cov_targets` session-scoped fixtures. These are deferred because pytest fixtures cannot be invoked from hooks (`pytest_sessionstart`/`pytest_sessionfinish`), and the fixture-to-hook bridge adds significant complexity. For v1, users run `sam build` before tests and configure discovery via `pyproject.toml`. These fixtures are a natural v2 enhancement.

**Files:**
- Modify: `src/pytest_cov_container/__init__.py`
- Create: `tests/test_public_api.py`
- Verify end-to-end import chain

- [ ] **Step 1: Write failing tests for public API**

```python
# tests/test_public_api.py
from unittest.mock import MagicMock

import pytest

import pytest_cov_container


class TestCollectContainerCoverage:
    def teardown_method(self):
        pytest_cov_container._active_plugin = None

    def test_warns_when_plugin_not_active(self):
        pytest_cov_container._active_plugin = None
        with pytest.warns(UserWarning, match="not active"):
            pytest_cov_container.collect_container_coverage()

    def test_calls_collect_from_running_when_active(self):
        mock_plugin = MagicMock()
        pytest_cov_container._active_plugin = mock_plugin
        pytest_cov_container.collect_container_coverage()
        mock_plugin.collect_from_running.assert_called_once()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `hatch test -- tests/test_public_api.py -v`
Expected: FAIL — `collect_container_coverage` not defined or `_active_plugin` not found

- [ ] **Step 3: Write __init__.py**

```python
# src/pytest_cov_container/__init__.py
# SPDX-FileCopyrightText: 2026-present tomaDev <genins21@gmail.com>
#
# SPDX-License-Identifier: MIT
import warnings
from typing import Optional


_active_plugin: Optional[object] = None


def collect_container_coverage():
    """Flush and collect coverage from running containers.

    Call this before stopping containers to capture mid-session coverage.

    Example::

        from pytest_cov_container import collect_container_coverage

        @pytest.fixture(scope="session")
        def sam_api():
            proc = start_sam(...)
            yield SAM_URL
            collect_container_coverage()
            proc.terminate()
    """
    if _active_plugin is None:
        warnings.warn(
            "pytest-cov-container is not active. "
            "Ensure pytest-cov is running (--cov) and "
            "[tool.pytest-cov-container] is configured.",
            UserWarning,
            stacklevel=2,
        )
        return
    _active_plugin.collect_from_running()
```

- [ ] **Step 2: Verify full import chain**

Run: `hatch run python -c "from pytest_cov_container import collect_container_coverage; print('public API OK')"`
Expected: `public API OK`

Run: `hatch run python -c "from pytest_cov_container.models import ContainerInfo, DriverConfig, InjectionResult, LanguageDriver; print('models OK')"`
Expected: `models OK`

Run: `hatch run python -c "from pytest_cov_container.drivers.python import PythonDriver; print('driver OK')"`
Expected: `driver OK`

- [ ] **Step 3: Run full test suite**

Run: `hatch test -- -v`
Expected: all tests PASS

- [ ] **Step 4: Run ruff**

Run: `ruff check --fix src/ tests/ && ruff format src/ tests/`
Expected: no errors (fix any that appear)

- [ ] **Step 5: Run public API tests**

Run: `hatch test -- tests/test_public_api.py -v`
Expected: all 2 tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/pytest_cov_container/__init__.py tests/test_public_api.py
git commit -m "feat: expose collect_container_coverage() public API"
```

---

## Summary

| Task | Component | Tests |
|------|-----------|-------|
| 1 | Project setup (pyproject.toml, fixtures, dirs) | setup verification |
| 2 | Data models (ContainerInfo, DriverConfig, etc.) | import check |
| 3 | Configuration loading | 6 tests |
| 4 | Build system resolver | 3 tests |
| 5 | Docker backend | 7 tests |
| 6 | Python driver — injection | 5 tests |
| 7 | Python driver — collection | 4 tests |
| 8 | Driver discovery | 4 tests |
| 9 | Plugin hooks | 5 tests |
| 10 | Public API + wiring | 2 tests + full suite |
