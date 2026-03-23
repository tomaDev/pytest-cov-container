# pytest-cov-container Design Spec

A pytest plugin that collects code coverage from applications running inside Docker containers during integration tests, then combines it with local test coverage into a single report.

## Problem

When integration tests run outside a Docker container and hit an application inside it via HTTP, coverage tools like pytest-cov only measure the test code, not the application code. Collecting coverage from inside the container requires manual plumbing: injecting instrumentation, signaling the process to flush data, extracting files via `docker cp`, remapping paths, and combining reports. Every team writes their own fragile conftest boilerplate for this.

No existing tool solves this. Codecov, Coveralls, and SonarQube are reporting services that consume coverage files — they don't collect them. No pytest plugin, Docker feature, or container-native tool addresses container coverage extraction.

## Solution

`pytest-cov-container` bridges the gap between pytest-cov and code running inside Docker containers. It handles the full lifecycle: instrumentation injection, container discovery, coverage data extraction, path normalization, and report combining.

## Scope

**v1 targets:** Python applications running in AWS SAM local containers using the Lambda Web Adapter pattern (custom `run.sh` entrypoint with uvicorn/gunicorn/etc.). Standard Lambda RIC-based handlers are out of scope for v1.

## Package

- **Name:** `pytest-cov-container`
- **Management:** hatch
- **Python driver:** built in (Python is the primary use case and the plugin is written in Python)
- **Future language drivers:** optional extras — `pytest-cov-container[go]`, `pytest-cov-container[node]`
- **Dependencies:** `pytest`, `pytest-cov`, `docker` (Python Docker SDK), `coverage`
- **Minimum coverage.py version:** 7.2+ (required for `sigterm = true` and `relative_files = true`)

## Architecture

### Approach: Hybrid Auto + Fixture Override

Default behavior is fully automatic via `pyproject.toml` config. The plugin hooks into pytest session lifecycle and handles everything. For advanced cases, users can override container targeting via a fixture.

### Core Components

#### 1. Docker Backend

All container interaction uses the Docker SDK for Python — no shelling out to the Docker CLI.

```python
class DockerBackend:
    def find_containers(self, image_pattern: str) -> list[ContainerInfo]:
        """List running containers matching the image pattern."""
        # Uses client.containers.list() with ancestor/name filtering

    def send_signal(self, container_id: str, signal: int) -> None:
        """Send a signal to the coverage-instrumented process."""
        # Uses container.exec_run() with /proc walk to find the right PID
        # (Lambda containers lack the kill binary, so we walk /proc)

    def extract_file(self, container_id: str, path: str, dest: Path) -> None:
        """Copy a file from the container to the host."""
        # Uses container.get_archive() — returns tar stream

    def inspect(self, container_id: str) -> dict:
        """Get full container metadata."""
        # Uses container.attrs
```

The `/proc` walk to send signals is executed via `container.exec_run()` (Docker SDK), not by shelling out to `docker exec`:

```python
container.exec_run(
    ["sh", "-c",
     "for p in /proc/[0-9]*/cmdline; do "
     "pid=$(echo $p | cut -d/ -f3); "
     "if grep -q _cov_wrapper $p 2>/dev/null; then kill -USR1 $pid; break; fi; done"]
)
```

Future container platforms (Podman, Kubernetes) would implement the same interface.

#### 2. Language Driver Interface

All language drivers — including the built-in Python driver — register via entry points and implement the same two-method protocol. This makes the Python driver a reference implementation for adding new languages.

```python
class LanguageDriver(Protocol):
    name: str  # "python", "go", etc.

    def inject(self, target_dir: Path, config: DriverConfig) -> InjectionResult:
        """Set up coverage instrumentation in target_dir."""

    def collect(self, docker: DockerBackend, container: ContainerInfo,
                dest: Path, config: DriverConfig) -> Path:
        """Signal the container, extract coverage data, normalize paths.

        Owns the full collection flow: send signal, wait, extract files,
        remap paths. Returns path to the ready-to-combine coverage data.
        Each language handles this differently (e.g. Python uses SIGUSR1 +
        coverage combine, Go copies GOCOVERDIR + go tool covdata).
        """
```

Entry point registration:
```toml
[project.entry-points."pytest_cov_container.drivers"]
python = "pytest_cov_container.drivers.python:PythonDriver"

# Future drivers, gated behind optional extras:
# go = "pytest_cov_container.drivers.go:GoDriver"
# node = "pytest_cov_container.drivers.node:NodeDriver"
```

The core discovers all registered drivers at startup via `importlib.metadata.entry_points()`. For v1, only the Python driver is registered. The user selects the driver via config (defaults to `python`):

```toml
[tool.pytest-cov-container]
language = "python"  # default, can be omitted
```

#### 3. Injection Result

Tracks what was written for potential cleanup.

```python
@dataclass
class InjectionResult:
    files_written: list[Path]     # new files created
    env_vars: dict[str, str]      # env vars the container needs (e.g. COVERAGE_PROCESS_START)
```

`env_vars` is informational — the plugin logs which env vars must be set but does not inject them into the container runtime. See Prerequisites.

#### 4. Build System Resolver

For v1, a simple config-driven path resolution. The abstraction exists so future resolvers (source-dir mode, Docker build context) can be added without rearchitecting.

```python
class SamBuildResolver:
    def resolve_target_dir(self, config: dict) -> Path:
        return Path(config.get("build_dir", ".aws-sam/build/ApiFunction"))
```

### Container Discovery

Two composable discovery filters:

**1. Docker label** — the Docker-native approach. User adds a label when starting the container:

```bash
docker run --label pytest-cov-container=true myapp
```

Or in `docker-compose.yml`:
```yaml
services:
  myapp:
    labels:
      pytest-cov-container: "true"
```

Config:
```toml
[tool.pytest-cov-container]
label = "pytest-cov-container"  # default, can be omitted
```

The Docker SDK supports filtering by label directly: `client.containers.list(filters={'label': 'pytest-cov-container'})`.

**2. Image pattern** — for tools like SAM local that don't support custom Docker labels. Matches the Docker image name against a glob pattern:

```toml
[tool.pytest-cov-container]
image_pattern = "samcli/lambda*"
```

The filters are composable: if both `label` and `image_pattern` are configured, a container must match **both** (AND logic). Either filter can be used alone. At least one must be configured. If no running container matches, the plugin warns.

All matching containers are signaled and their coverage data is extracted to a single flat temp directory. No collisions occur because `parallel = true` gives each file a unique suffix (hostname + pid + random). `coverage combine` merges them all.

**3. Fixture override** — for advanced cases, users can override discovery entirely:

```python
@pytest.fixture(scope="session")
def container_cov_targets():
    return [
        {
            "image_pattern": "samcli/lambda*",
            "language": "python",
            "path_mapping": {"src/api": "/var/task"},
        },
    ]
```

### Coverage Data Saving

Two mechanisms:

- **SIGTERM (default)** — coverage.py's native `sigterm = true` saves data when the container stops. `docker cp` / `get_archive()` works on stopped containers, so the plugin extracts after exit. Fully automatic, no user code.
- **SIGUSR1 (optional)** — for mid-session flushes from long-running containers. The wrapper registers a SIGUSR1 handler. The plugin sends it via `container.exec_run()` with a `/proc` walk (Lambda containers lack the `kill` binary).

### Python Driver Details

**Injection writes three files into the SAM build directory:**

1. **`.coveragerc`** — auto-generated config:
   ```ini
   [run]
   data_file = /tmp/.coverage.container
   relative_files = true
   parallel = true
   sigterm = true
   include =
       *.py
   omit =
       _cov_wrapper.py
   ```
   The `include = *.py` combined with `relative_files = true` measures all Python files in the working directory (`/var/task/`). Third-party packages are installed in subdirectories (e.g. `site-packages/`) and are excluded by coverage.py's default behavior when `source` is not set. The `omit` excludes the wrapper itself.

2. **`_cov_wrapper.py`** — shim that starts coverage, registers a SIGUSR1 handler for on-demand flushing, then runs the app entrypoint as a child process. The entrypoint is run via `subprocess.Popen` (not `os.execvp`) so the wrapper process stays alive to handle signals. Any valid CLI command works (`uvicorn ...`, `gunicorn ...`, `python -m ...`):
   ```python
   import os, signal, subprocess, coverage

   cov = coverage.Coverage(config_file=os.environ.get(
       "COVERAGE_PROCESS_START", "/var/task/.coveragerc"))
   cov.start()

   def _flush(*_):
       cov.save()

   signal.signal(signal.SIGUSR1, _flush)

   proc = subprocess.Popen(["sh", "-c", os.environ.get(
       "CONTAINER_COV_ENTRYPOINT",
       "uvicorn app:app --host 0.0.0.0 --port 8080")])
   proc.wait()
   cov.stop()
   cov.save()
   ```
   The entrypoint command is read from the `CONTAINER_COV_ENTRYPOINT` env var at runtime (set via SAM `env.json`), with the config value baked in as a default fallback.

3. **Modified `run.sh`** — the Lambda Web Adapter entrypoint. The plugin writes a `run.sh` that unconditionally launches via `_cov_wrapper.py`:
   ```bash
   #!/bin/bash
   exec python /var/task/_cov_wrapper.py
   ```
   This is unconditional (not gated on `COVERAGE_PROCESS_START`) because the plugin only writes it when `--container-cov` is active. The original `run.sh` is overwritten in the build dir, which is ephemeral and regenerated by `sam build`.

### Prerequisites

1. **`coverage` must be a dependency** of the Lambda function (in `pyproject.toml` / `requirements.txt`) so `sam build` installs it into the build artifact. The plugin does not install packages.

2. **`sam build` must be run before tests.** The plugin patches the build output, not the source tree. Users can automate this by overriding the `container_cov_build` fixture (see below). Otherwise, the build dir must already exist. (Source-dir injection is a future extension — the `InjectionResult` and `BuildSystemResolver` design supports this without rearchitecting.)

The plugin provides a no-op `container_cov_build` fixture that runs before injection. Users override it to automate their build step:

```python
# In user's conftest.py:
@pytest.fixture(scope="session")
def container_cov_build():
    subprocess.run(["sam", "build", "--beta-features"], check=True)
```

If not overridden, the plugin expects the build dir to already exist and errors with a helpful message if it doesn't.

3. **`COVERAGE_PROCESS_START` env var** should be set in the SAM `env.json` pointing to `/var/task/.coveragerc`. This tells coverage.py where its config is. Example:
   ```json
   {
     "ApiFunction": {
       "COVERAGE_PROCESS_START": "/var/task/.coveragerc"
     }
   }
   ```
   Note: The `_cov_wrapper.py` falls back to `/var/task/.coveragerc` if the env var is not set, so this is optional but recommended for consistency with coverage.py's subprocess support.

### Extraction & Collection

**Default flow (fully automatic):** The container stops during fixture teardown (e.g. `proc.terminate()`), SIGTERM triggers coverage.py to save data, and the plugin extracts from the stopped container in `pytest_sessionfinish`. No user code needed.

**Mid-session flow (optional):** For long-running containers that outlive the test session, the plugin also exposes `collect_container_coverage()` which sends SIGUSR1 to flush data from a running container:

```python
from pytest_cov_container import collect_container_coverage

@pytest.fixture(scope="session")
def sam_api(aws_resources, mock_bedrock_server):
    proc = start_sam(...)
    yield SAM_URL
    collect_container_coverage()  # optional: flush from running container
    proc.terminate()
```

#### Combine step

Runs in `pytest_sessionfinish(trylast=True)`:

1. Finds matching containers (running or stopped) and extracts coverage data
2. Generates a temporary `.coveragerc` with `[paths]` mapping from user config
3. Runs `coverage combine` to merge container + local coverage
4. The merged `.coverage` file is ready for `coverage report` / `coverage html`

### Path Normalization

The `normalize()` method generates a temporary `.coveragerc` with a `[paths]` section derived from the user's `path_mapping` config:

```ini
[paths]
source =
    src/api
    /var/task
```

Combined with `relative_files = true` in the container's `.coveragerc`, coverage.py stores paths like `app.py` rather than `/var/task/app.py`. The `[paths]` section tells `coverage combine` that `src/api/app.py` and `app.py` (from the container) refer to the same source. This is coverage.py's built-in mechanism for cross-environment path reconciliation.

**Error handling:**
- Container not found: warning, not test failure
- Signal timeout: retry once, then warn
- Extract fails: warn with instructions to check container status
- Combine fails: save raw container coverage file for manual recovery

## Configuration

```toml
[tool.pytest-cov-container]
# Container discovery (glob matched against Docker image name)
image_pattern = "samcli/lambda*"

# Path mapping: host source path -> container path
[tool.pytest-cov-container.path_mapping]
"src/api" = "/var/task"

# Python driver config
[tool.pytest-cov-container.python]
build_dir = ".aws-sam/build/ApiFunction"  # supports globs, e.g. ".aws-sam/build/*"
entrypoint = "uvicorn app:app --host 0.0.0.0 --port 8080"
```

The plugin activates automatically when pytest-cov is running (`--cov`) and `[tool.pytest-cov-container]` config exists in `pyproject.toml`. No extra flag needed.

Can be disabled via:
- `--no-cov-container` CLI flag
- `container_cov = false` in `pyproject.toml`

```toml
[tool.pytest-cov-container]
enabled = true  # default, can be set to false to disable
```

## Plugin Lifecycle

### pytest_configure
- Register `--no-cov-container` CLI flag and ini options
- Check if pytest-cov is active and `[tool.pytest-cov-container]` config exists
- If both true and not disabled: activate the plugin

### pytest_sessionstart
- If `--container-cov` is active:
  - `container_cov_build` fixture runs first (no-op by default, user overrides for build automation)
  - Resolve target directory via build system resolver
  - Verify build directory exists (error with helpful message if not)
  - Run Python driver's `inject()` to write instrumentation files

### pytest_sessionfinish (trylast=True)
- Find matching containers (running or stopped) by label, image pattern, or fixture override
- For each container, call `driver.collect()` which handles extraction and normalization
- Run `coverage combine` to merge container + local coverage
- Final `.coverage` file is ready for reporting

## User Experience

**One-time setup:**
1. `uv pip install pytest-cov-container`
2. Add `coverage` to Lambda function dependencies
3. Add `[tool.pytest-cov-container]` config to `pyproject.toml`

**Test workflow:**
```bash
sam build
pytest --cov=src/api
```

**What this replaces in user conftest:**
- `_inject_coverage` fixture
- `_find_sam_container()` function
- `_collect_integration_coverage()` function
- `_COV_WRAPPER` / `_RUN_SH_WITH_COVERAGE` / `_make_coveragerc()` templates

The user's conftest shrinks to: start mocks, seed data, start SAM, yield URL.

## Future Extensions

- **Source-dir injection:** inject into source tree instead of build dir (for `sam local` without `sam build`). The `BuildSystemResolver` and `InjectionResult` design supports this without rearchitecting.
- **Standard Lambda RIC support:** inject coverage via `sitecustomize.py` and handler wrapping instead of `run.sh` modification.
- **Go driver:** `pytest-cov-container[go]` extra. Uses `GOCOVERDIR` env var and `go tool covdata` for extraction. Registers via the same entry point mechanism as the Python driver.
- **Node driver:** `pytest-cov-container[node]` extra. Uses NYC/Istanbul `.nyc_output`. Same registration pattern.
- **Other container platforms:** Podman, Kubernetes (`kubectl exec`/`kubectl cp`), ECS Exec. The `DockerBackend` interface is designed for additional implementations.
