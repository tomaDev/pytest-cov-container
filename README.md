# pytest-cov-container

[![PyPI - Version](https://img.shields.io/pypi/v/pytest-cov-container.svg)](https://pypi.org/project/pytest-cov-container)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/pytest-cov-container.svg)](https://pypi.org/project/pytest-cov-container)
[![security: bandit](https://img.shields.io/badge/security-bandit-yellow.svg)](https://github.com/PyCQA/bandit)

Collect code coverage from Python applications running inside Docker containers during integration tests. Works alongside [pytest-cov](https://github.com/pytest-dev/pytest-cov) to combine container coverage with your local test coverage.

Built for projects using [AWS SAM](https://docs.aws.amazon.com/serverless-application-model/) local testing, but works with any Docker-based test workflow.

## How It Works

1. **Before tests** — injects a coverage wrapper, `.coveragerc`, and `run.sh` into your SAM build directory
2. **During tests** — your containerized app runs under `coverage` via the injected wrapper
3. **After tests** — extracts `.coverage.*` files from containers and runs `coverage combine` to merge them with local results

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
entrypoint = "uvicorn app:app --host 0.0.0.0 --port 8080"
```

| Key | Description |
|-----|-------------|
| `image_pattern` | Glob pattern to match container image tags |
| `label` | Docker label to filter containers |
| `path_mapping` | Maps host source paths to container paths (used by `coverage combine`) |
| `build_dir` | SAM build output directory where coverage files are injected |
| `entrypoint` | The command your app runs inside the container |
| `language` | Language driver to use (default: `"python"`) |
| `enabled` | Set to `false` to disable (default: `true`) |

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

- Writing `.coveragerc` with `parallel = true` and `sigterm = true`
- Writing `_cov_wrapper.py` that starts coverage, handles `SIGTERM`/`SIGUSR1`, and runs your entrypoint
- Writing `run.sh` to bootstrap the wrapper
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
