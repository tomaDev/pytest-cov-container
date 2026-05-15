# pytest-cov-container

[![PyPI - Version](https://img.shields.io/pypi/v/pytest-cov-container.svg)](https://pypi.org/project/pytest-cov-container)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/pytest-cov-container.svg)](https://pypi.org/project/pytest-cov-container)
[![security: bandit](https://img.shields.io/badge/security-bandit-yellow.svg)](https://github.com/PyCQA/bandit)

Collect code coverage from Python applications running inside Docker containers during integration tests. Works alongside [pytest-cov](https://github.com/pytest-dev/pytest-cov) to combine container coverage with your local test coverage.

Built for projects using [AWS SAM](https://docs.aws.amazon.com/serverless-application-model/) local testing, but works with any Docker-based test workflow.

## How It Works

1. **Before tests** — moves your `<build_dir>/run.sh` aside to `_orig_run.sh`, then injects a coverage wrapper, `.coveragerc`, and a shim `run.sh` that exec's the wrapper. The wrapper invokes your unmodified `_orig_run.sh` under `coverage`. Your production entrypoint is the test entrypoint by construction — no duplicate command string to drift.
2. **During tests** — your containerized app runs under `coverage` via the injected wrapper, which forwards `SIGTERM` to your app on container shutdown so coverage data is saved cleanly.
3. **After tests** — extracts `.coverage.*` files from containers and runs `coverage combine` to merge them with local results.

## Installation

```console
pip install pytest-cov-container
```

Requires Python 3.11+.

## Configuration

Add to your `pyproject.toml`:

```toml
[tool.pytest-cov-container]
image_pattern = "samcli/lambda*"
label = "pytest-cov-container"

[tool.pytest-cov-container.path_mapping]
"src/api" = "/var/task"

[tool.pytest-cov-container.python]
build_dir = ".aws-sam/build/ApiFunction"
```

| Key | Description |
|-----|-------------|
| `image_pattern` | Glob pattern to match container image tags |
| `label` | Docker label to filter containers |
| `path_mapping` | Maps host source paths to container paths (used by `coverage combine`) |
| `build_dir` | SAM build output directory where coverage files are injected |
| `entrypoint` | **Override (discouraged).** Replace convention-discovered `<build_dir>/run.sh` with this command via `sh -c`. Drift between this string and prod `run.sh` is the bug class this plugin's default path eliminates. Omit the field to use convention discovery. Setting to the empty string raises a load-time error. |
| `language` | Language driver to use (default: `"python"`) |
| `enabled` | Set to `false` to disable (default: `true`) |

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

This sends `SIGUSR1` to running containers to flush coverage data, then extracts the files.

## Pluggable Drivers

Language support is pluggable via entry points. The built-in Python driver handles:

- Moving your `<build_dir>/run.sh` aside to `_orig_run.sh` (mode preserved, idempotent across re-runs)
- Writing `.coveragerc` with `parallel = true` and `sigterm = true`
- Writing `_cov_wrapper.py` that starts coverage, splits `SIGUSR1`/`SIGTERM` handling (save-only vs save+forward), forwards `SIGTERM` to the child process so the runtime grace period saves cleanly, and invokes your unmodified `_orig_run.sh`
- Writing a shim `run.sh` that exec's the wrapper
- Extracting `.coverage.*` files from `/tmp` in containers

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
```

## License

`pytest-cov-container` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
