# pytest-cov-container

[![PyPI - Version](https://img.shields.io/pypi/v/pytest-cov-container.svg)](https://pypi.org/project/pytest-cov-container)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/pytest-cov-container.svg)](https://pypi.org/project/pytest-cov-container)
[![security: bandit](https://img.shields.io/badge/security-bandit-yellow.svg)](https://github.com/PyCQA/bandit)

Collect code coverage from Python applications running inside Docker containers during integration tests. Works alongside [pytest-cov](https://github.com/pytest-dev/pytest-cov) to combine container coverage with your local test coverage.

Built for [AWS SAM](https://docs.aws.amazon.com/serverless-application-model/) local testing: by default it collects from the Lambda containers `sam local` starts for this checkout.

## How It Works

1. **Before tests** — moves your `<build_dir>/run.sh` aside to `_orig_run.sh`, then injects a coverage wrapper, `.coveragerc`, and a shim `run.sh` that exec's the wrapper. The wrapper invokes your unmodified `_orig_run.sh` under `coverage`. Your production entrypoint is the test entrypoint by construction — no duplicate command string to drift.
2. **During tests** — your containerized app runs under `coverage` via the injected wrapper, which forwards `SIGTERM` to your app on container shutdown so coverage data is saved cleanly.
3. **After tests** — signals each owned container's coverage process to save, waits for its save sentinel, copies the data files out and drops them next to pytest-cov's data file. pytest-cov's own combine merges them with the host data, so its report and `--cov-fail-under` include container coverage.

## Installation

```console
pip install pytest-cov-container
```

Requires Python 3.11+ and, for the default container selection, SAM CLI 1.165+ (the first release that labels its Lambda containers).

## Configuration

Add to your `pyproject.toml`:

```toml
[tool.pytest-cov-container]

[tool.pytest-cov-container.path_mapping]
"src/api" = "/var/task"

[tool.pytest-cov-container.python]
build_dir = ".aws-sam/build/ApiFunction"
```

| Key | Description |
|-----|-------------|
| `image_pattern` | Glob pattern, or list of patterns, matched against the image the container was created from (then its tags) |
| `label` | Docker label filter, `key` or `key=value` (default: `sam.cli.container.type=lambda`, which SAM CLI 1.165+ sets on every Lambda container; `""` switches the filter off) |
| `mount_prefix` | Ownership: only containers that bind-mount a directory under `<rootdir>/<mount_prefix>/` (default: the parent of `build_dir`, i.e. the SAM build root; `""` switches the check off). Tells concurrent checkouts apart, which image and labels cannot: they are the same in every checkout. `sam local` mounts each function's build dir. |
| `worker_env` | Ownership: only containers whose env holds `<worker_env>=<worker id>` (`gw0`, `gw1`, … under xdist, else `main`). Pass `container_env()` into the containers (see below). |
| `required` | Fail the run when a collection pass gets no data (default: `false`; also `--cov-container-required`) |
| `path_mapping` | Maps host source paths to container paths; applied when each collected file is handed to pytest-cov. `[tool.coverage.paths]` works too. |
| `language` | Language driver to use (default: `"python"`) |
| `enabled` | Set to `false` to disable (default: `true`) |

`[tool.pytest-cov-container.python]`:

| Key | Description |
|-----|-------------|
| `build_dir` | SAM build output directory where coverage files are injected (default: `.aws-sam/build/ApiFunction`) |
| `wrapper` | `false`: your application starts coverage itself and runs the save protocol; only `.coveragerc` is injected and `run.sh` is left alone (default: `true`) |
| `source_dir` | Measure only this host directory's `*.py` files (minus `tests/`, `__pycache__/` and dot-dirs), at their paths under `container_root`. Keeps vendored dependencies in the build dir unmeasured. Default: every `*.py`. |
| `container_root` | Where `build_dir` is mounted in the container (default: `/var/task`) |
| `branch` | Branch coverage in the container. Default: the host's setting, which it must match to combine. |
| `relative_files` | coverage's `relative_files` in the container (default: `true`; set `false` to record absolute container paths for `[paths]` remapping) |
| `entrypoint` | **Override (discouraged).** Replace convention-discovered `<build_dir>/run.sh` with this command via `sh -c`. Drift between this string and prod `run.sh` is the bug class this plugin's default path eliminates. Omit the field to use convention discovery. Setting to the empty string raises a load-time error. |

### Default Path: Convention Discovery

The plugin reads your existing `<build_dir>/run.sh` (whatever `sam build` produced) and arranges for coverage to wrap it. Requirements:

- `<build_dir>/run.sh` must exist after `sam build`.
- Your build_dir must include the `coverage` package as an installed dependency (the plugin checks for a `coverage*.pth` file under any `site-packages` directory below `build_dir`). This is what enables subprocess coverage attach.

If either precondition fails, `inject()` raises with a migration hint.

## Usage

Run your tests with `--cov` as usual:

```console
pytest --cov=src/api tests/
```

The plugin activates automatically when:
- pytest-cov is active (`--cov` flag present)
- `[tool.pytest-cov-container]` is configured in `pyproject.toml`

Disable it for a run with:

```console
pytest --cov=src/api --no-cov-container tests/
```

### Mid-Session Collection

If you need to collect coverage before containers stop (e.g., in a session-scoped fixture teardown), use the public API:

```python
from pytest_cov_container import collect_container_coverage

@pytest.fixture(scope="session")
def sam_api():
    proc = start_sam(...)
    yield SAM_URL
    collect_container_coverage()
    proc.terminate()
```

This saves and collects the running containers this process owns and returns the number of data files collected. With `required`, collecting nothing raises. Containers still around after the tests are collected automatically before pytest-cov reports.

The wrapper's `SIGUSR1` save flushes the wrapper process only. A long-running server started by `_orig_run.sh` saves when it gets `SIGTERM` (container stop), so for mid-session collection from a server, set `wrapper = false` and run the save protocol in the application.

### Save Protocol

The process that owns the `coverage.Coverage` object follows three rules:

1. After `cov.start()`, write its pid to `PID_FILE` (`/tmp/.cov_container.pid`).
2. On `SIGUSR1`, call `cov.save()`, then create `DONE_FILE` (`/tmp/.cov_container.done`).
3. Read the injected `.coveragerc` (path in `COVERAGE_PROCESS_START`), which writes data files as `/tmp/.coverage.container*`.

With `wrapper = false`, the application does this itself, for example:

```python
import os
import signal
from pathlib import Path


def maybe_start_coverage() -> None:
    rcfile = os.environ.get("COVERAGE_PROCESS_START")
    if not rcfile:
        return  # production: no coverage
    import coverage

    cov = coverage.Coverage(config_file=rcfile)
    cov.start()
    Path("/tmp/.cov_container.pid").write_text(str(os.getpid()))

    def save(*_):
        cov.save()
        Path("/tmp/.cov_container.done").touch()

    signal.signal(signal.SIGUSR1, save)
```

### pytest-xdist and Concurrent Checkouts

Image and label say what a container is, not whose it is. Two xdist workers, or two checkouts of the project, run identical images. Set the ownership keys and pass the marker into the containers:

```toml
[tool.pytest-cov-container]
worker_env = "MY_TEST_WORKER"  # label and mount_prefix keep their SAM defaults
```

```python
import pytest_cov_container

env_vars = {"Parameters": pytest_cov_container.container_env(project_root)}
# write env_vars to env.json, then: sam local start-api --env-vars env.json
```

`container_env()` returns the worker marker and, while the plugin is active, `COVERAGE_PROCESS_START`. `sam local --env-vars` only overrides variables the template declares, so declare both in `template.yaml` (empty values are fine) or sam drops them silently.

Under xdist the controller injects before any worker starts, each worker collects only its own containers, and pytest-cov's controller combines everything once.

`owned_containers(rootpath, all_workers=False)` lists the same containers for other uses, such as a reaper that removes leaked containers without touching a concurrent session's.

## Pluggable Drivers

Language support is pluggable via entry points. The built-in Python driver handles:

- Moving your `<build_dir>/run.sh` aside to `_orig_run.sh` (mode preserved, idempotent across re-runs)
- Writing `.coveragerc` with `parallel = true`, `sigterm = true` and the host's `branch` setting
- Writing `_cov_wrapper.py` that starts coverage, runs the save protocol, splits `SIGUSR1`/`SIGTERM` handling (save-only vs save+forward), forwards `SIGTERM` to the child process so the runtime grace period saves cleanly, and invokes your unmodified `_orig_run.sh`
- Writing a shim `run.sh` that exec's the wrapper
- Extracting `.coverage.container*` files from `/tmp` in containers

All injected files are written atomically.

To add a driver for another language, register an entry point:

```toml
[project.entry-points."pytest_cov_container.drivers"]
node = "my_package.drivers.node:NodeDriver"
```

Drivers must implement the `LanguageDriver` protocol from `pytest_cov_container.models`.

## Development

```console
# Run tests
hatch test

# Run across all Python versions
hatch test --all

# Format and lint
hatch fmt

# Type check
hatch run types:check

# Security scan
hatch run security:scan

# Cut a release: bump + commit + tag + push in one shot. Pass a version
# literal or a hatch segment (`patch`, `minor`, `major`, `rc`, etc.).
# Omit the arg to bump the minor version.
hatch run release           # 0.2.0 → 0.3.0
hatch run release patch     # 0.2.0 → 0.2.1
hatch run release 0.3.5     # explicit
```

## License

`pytest-cov-container` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
