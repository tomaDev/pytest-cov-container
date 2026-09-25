# pytest-cov-container

[![PyPI - Version](https://img.shields.io/pypi/v/pytest-cov-container.svg)](https://pypi.org/project/pytest-cov-container)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/pytest-cov-container.svg)](https://pypi.org/project/pytest-cov-container)
[![security: bandit](https://img.shields.io/badge/security-bandit-yellow.svg)](https://github.com/PyCQA/bandit)

Collect code coverage from Python applications running inside Docker containers during integration tests. Works alongside [pytest-cov](https://github.com/pytest-dev/pytest-cov) to combine container coverage with your local test coverage.

You name your framework and the plugin infers the rest. Supported today: [AWS SAM](https://docs.aws.amazon.com/serverless-application-model/) (`sam local`).

## How It Works

1. **Before tests** — for every function it measures, the plugin injects into the function's build dir a `.coveragerc`, a pure-Python copy of `coverage`, a small bootstrap module and a `.pth` file that imports it. The Lambda Python runtime processes `.pth` files in the task root before it imports your handler, so coverage starts before your code loads. Your build and your entrypoint stay untouched.
2. **During tests** — each pytest process (each xdist worker) runs a small HTTP sink. `container_env()` hands the containers its URL. The bootstrap pushes the process's coverage data to it after every handler call (`sam local invoke` removes its container right after the invoke, so there is no later chance) and, for a long-running server, when the plugin sends `SIGUSR1`.
3. **After tests** — each push becomes a suffix file of pytest-cov's data file, with container paths mapped back to your sources. pytest-cov's own combine merges them with the host data, so its report and `--cov-fail-under` include container coverage.

## Installation

```console
pip install pytest-cov-container
```

Requires Python 3.11+. For `framework = "aws-sam"`: SAM CLI 1.165+ (the first release that labels its Lambda containers).

## Configuration

Add to your `pyproject.toml`:

```toml
[tool.pytest-cov-container]
framework = "aws-sam"
```

`framework` is required. It selects a preset that works out everything below from your project; an explicit key always wins.

For `framework = "aws-sam"` the preset reads `template.yaml`:

| What | Inferred as |
|-----|-------------|
| functions | every zip Python function with a local `CodeUri` (or the `functions` list) |
| build dir | from `sam build`'s own template (`.aws-sam/build/<dir>`; functions that share code share one), else `.aws-sam/build/<logical id>` |
| sources | the function's `CodeUri` at `/var/task`, and each local layer's `ContentUri` at `/opt` (Globals and function `Layers`) |
| `label` | `sam.cli.container.type=lambda` (set by SAM CLI 1.165+ on every Lambda container) |
| `mount_prefix` | the build root: only this checkout's containers |

| Key | Description |
|-----|-------------|
| `framework` | Preset that infers the defaults (supported: `aws-sam`) |
| `functions` | `aws-sam`: logical ids to measure (default: every eligible function) |
| `template` | `aws-sam`: SAM template, relative to rootdir (default: `template.yaml`) |
| `build_dir` | `aws-sam`: the SAM build root (default: `.aws-sam/build`) |
| `worker_env` | Ownership: only containers whose env holds `<worker_env>=<worker id>` (`gw0`, `gw1`, … under xdist, else `main`) |
| `label` | Docker label filter, `key` or `key=value`; `""` switches the filter off |
| `mount_prefix` | Ownership: only containers that bind-mount a directory under `<rootdir>/<mount_prefix>/`; `""` switches the check off. Tells concurrent checkouts apart, which image and labels cannot: they are the same in every checkout. |
| `image_pattern` | Glob pattern, or list of patterns, matched against the image the container was created from (then its tags) |
| `branch` | Branch coverage in the containers. Default: the host's setting, which it must match to combine. |
| `sink_host` | Address containers use to reach the sink (default: detected, see below) |
| `sink_bind` | Address the sink listens on (default: detected, see below) |
| `docker_network` | The docker network the containers join, for that detection (default: `bridge`) |
| `required` | Fail when a process that flushed containers received no coverage (default: `false`; also `--cov-container-required`) |
| `enabled` | Set to `false` to disable (default: `true`) |

### Template declarations

`sam local --env-vars` only overrides variables the template declares, so declare the bootstrap's two variables (and your `worker_env`, if any) empty in `Globals`. Empty, the injected bootstrap does nothing, so production is unaffected:

```yaml
Globals:
  Function:
    Environment:
      Variables:
        COVERAGE_PROCESS_START: ""
        COV_CONTAINER_SINK: ""
```

A process that the Lambda runtime does not start, such as a web server behind the Lambda Web Adapter (`run.sh`), must process the task root's `.pth` files itself, as the runtime would:

```python
import os
import site

if task_root := os.environ.get("LAMBDA_TASK_ROOT"):
    site.addsitedir(task_root)
```

## Usage

Run your tests with `--cov` as usual:

```console
pytest --cov=src tests/
```

The plugin activates automatically when pytest-cov is active (`--cov`) and `[tool.pytest-cov-container]` is configured. Disable it for a run with `--no-cov-container`.

Pass `container_env()` into the containers, for example in `sam local`'s env file:

```python
import pytest_cov_container

env_vars = {"Parameters": pytest_cov_container.container_env(project_root)}
# write env_vars to env.json, then: sam local start-api --env-vars env.json
```

### Long-running servers

A handler pushes after every call by itself. A long-running server (a web app behind the Lambda Web Adapter) pushes when asked: call `collect_container_coverage()` before stopping its containers.

```python
from pytest_cov_container import collect_container_coverage


@pytest.fixture(scope="session")
def sam_api():
    proc = start_sam(...)
    yield SAM_URL
    collect_container_coverage()
    proc.terminate()
```

It signals the running containers this process owns and waits for their pushes; containers still running when the tests end are flushed automatically before pytest-cov reports.

### Reaching the Sink

The plugin asks the Docker engine where containers can reach the host:

- **Docker Desktop, OrbStack, or any engine on a macOS/Windows host** — the sink listens on `127.0.0.1` and containers use `host.docker.internal`, which these engines forward to the host's loopback.
- **A Linux engine** (CI runners) — the sink listens on the gateway of the containers' network (`docker0`, typically `172.17.0.1`) and containers use that IP, so no `--add-host` is needed and nothing outside Docker can connect.

Set `docker_network` when the containers join another network (`sam local --docker-network`), or `sink_bind` / `sink_host` for anything detection gets wrong (rootless Docker, a remote engine). Either wins over detection. The token in the sink's URL keeps anything but this session's containers from writing data.

### pytest-xdist and Concurrent Checkouts

Every process that runs tests has its own sink, and its containers push to it, so each worker receives exactly its own containers' data. Under xdist the controller injects before any worker starts, and pytest-cov's controller combines everything once.

Ownership still matters for flushing long-running servers and for cleanup: image and label say what a container is, not whose it is. Set `worker_env` and declare it in the template; the preset's `mount_prefix` tells checkouts apart. `owned_containers(rootpath, all_workers=False)` lists the owned containers for other uses, such as a reaper that removes leaked containers without touching a concurrent session's.

## Development

```console
# Install the pre-commit hooks (once per clone): checks, ty, bandit and
# `hatch test` run on every commit
prek install

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

# Cut a release: test every Python, then bump + commit + tag + push in one shot. Pass a version
# literal or a hatch segment (`patch`, `minor`, `major`, `rc`, etc.).
# Omit the arg to bump the minor version.
hatch run release           # 0.2.0 → 0.3.0
hatch run release patch     # 0.2.0 → 0.2.1
hatch run release 0.3.5     # explicit
```

## License

`pytest-cov-container` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
