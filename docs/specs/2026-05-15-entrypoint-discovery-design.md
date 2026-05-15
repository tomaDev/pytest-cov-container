# Entrypoint Discovery: Eliminate the Command-String Drift Class

**Status:** Proposed
**Date:** 2026-05-15
**Authors:** tomaDev (w/ assistance)

## Background

Today, `[tool.pytest-cov-container.python] entrypoint = "..."` in a consumer's `pyproject.toml` is a free-form command string that the Python driver substitutes into a generated `_cov_wrapper.py`. The wrapper spawns the user's application via `subprocess.Popen(["sh", "-c", "<entrypoint>"])`. The plugin then overwrites `<build_dir>/run.sh` with a one-liner that execs the wrapper.

This duplicates the user's actual production entrypoint. A consumer's prod Lambda runs `<build_dir>/run.sh` directly (whatever `sam build` produced). The plugin's test-mode `run.sh` invokes the same command via a *separately-declared* string in pyproject.toml. The two declarations can drift silently.

A real incident in the first consumer (chatbot-demo-2) demonstrated the failure mode: the prod `run.sh` used `python -m uvicorn "app:create_app(audience='${OGNATE_AUDIENCE}')" --factory ...` while the test-mode `entrypoint` string used `uvicorn app:app ...`. Tests passed. Every prod Lambda cold-start died at the ASGI import (`Attribute "create_app(audience='all')" not found in module "app"`). The drift surface in pytest-cov-container's current schema makes this class of bug expressible.

## Goal

Make plugin invoke the user's actual `<build_dir>/run.sh` so the production entrypoint is the test entrypoint by construction. Eliminate the command-string duplicate.

## Non-Goals

- New language drivers.
- Multi-container-per-function support.
- Automatic restoration of pre-injection state. `sam build` rerun is the canonical reset.
- Backward-compat shims for the `entrypoint = "..."` field beyond what is explicitly designed in.

## Constraints

- Plugin is on PyPI but in Alpha (`Development Status :: 3 - Alpha`). One known consumer. API breakage acceptable.
- Python ≥ 3.11.
- The plugin runs against a SAM build output by default (`.aws-sam/build/<Function>/`). Non-SAM users would use any build layout with a top-level `run.sh`.

## Approach: Move-and-Shim with Convention-Based Discovery

### Discovery

Plugin reads the user entrypoint from `<build_dir>/run.sh` by convention. No new config field is required in the common case.

### Override Hook (Opt-In Drift)

The existing `entrypoint = "..."` field is repurposed as an override:

- `entrypoint` absent (default) → convention discovery → move-and-shim path.
- `entrypoint` set to a non-empty string → legacy string-based path (today's behavior). Documented as discouraged; intended for cases where test-mode must differ from prod (e.g. fixture-injected port, mocked args).
- `entrypoint = ""` → load-time error. Prevents accidental silent fall-through to the default.

### Move-and-Shim Semantics

On `inject(build_dir, cfg)` in the default path:

1. Validate `<build_dir>/run.sh` exists and is readable. Raise with a migration hint otherwise.
2. If `<build_dir>/_orig_run.sh` does not exist, `shutil.copy2()` `run.sh` → `_orig_run.sh`. Mode preserved.
3. Always (idempotent): write `.coveragerc`, `_cov_wrapper.py`, `run.sh` (the shim). `chmod +x` on the two shell scripts.
4. Return `InjectionResult(files_written=[...], env_vars={"COVERAGE_PROCESS_START": "/var/task/.coveragerc"})`.

On re-injection (e.g. consecutive test runs without an intervening `sam build`), `_orig_run.sh`'s existence is the sentinel: the move step is skipped, the three config artifacts are refreshed. Idempotent.

If `_orig_run.sh` exists but `run.sh` does not look like the injected shim (a partial-failure recovery case), a warning is logged and both files are rewritten.

### Override Path (Legacy)

When `cfg.entrypoint` is a non-empty string:

1. No move. `_orig_run.sh` is not created.
2. Write `.coveragerc`, `_cov_wrapper.py` (legacy `sh -c <entrypoint>` template), `run.sh` (shim exec'ing wrapper). `chmod +x` on `run.sh`.
3. Same `InjectionResult` shape.

## Components

### `models.py`

```python
@dataclass
class DriverConfig:
    build_dir: str
    entrypoint: str | None = None        # was: str
    path_mapping: dict[str, str] = field(default_factory=dict)
```

`entrypoint` becomes optional. Default `None` signals "use convention discovery".

### `config.py`

`load_config()` reads `entrypoint` from pyproject when present; otherwise leaves it `None`. Empty string is rejected at load time:

```python
ep = driver_section.get("entrypoint")
if ep == "":
    raise ValueError(
        "[tool.pytest-cov-container.python].entrypoint is empty. "
        "Remove the field to use convention discovery, or set a non-empty command."
    )
```

The previous hardcoded default (`"uvicorn app:app --host 0.0.0.0 --port 8080"`) is removed.

### `drivers/python.py`

`PythonDriver.inject` branches on `config.entrypoint is None`. Two helper functions:

```python
def _inject_shim(target_dir: Path, config: DriverConfig) -> list[Path]:
    """Default move-and-shim path. Discovers run.sh by convention."""

def _inject_legacy(target_dir: Path, config: DriverConfig) -> list[Path]:
    """Override path. Generates wrapper that subprocess-spawns config.entrypoint."""
```

New `_cov_wrapper.py` template for the default path:

```python
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
signal.signal(signal.SIGTERM, _flush)

proc = subprocess.Popen(["bash", "/var/task/_orig_run.sh"])
rc = proc.wait()
cov.stop()
cov.save()
raise SystemExit(rc)
```

Differences from today:
- `subprocess.Popen` invokes the moved script directly (not `sh -c <string>`).
- SIGTERM gets an explicit handler in addition to coverage's own `sigterm = true` config.
- Exit code propagated to caller (today the wrapper silently discards it).
- `CONTAINER_COV_ENTRYPOINT` env-var override is **removed**. It was a runtime escape for the pyproject string; obsolete now that the source-of-truth is `_orig_run.sh`.

Legacy `_cov_wrapper.py` template (override path) retains today's `sh -c` semantics, with exit-code propagation added.

### `_RUN_SH_TEMPLATE`

Unchanged from today:

```bash
#!/bin/bash
exec python /var/task/_cov_wrapper.py
```

### `plugin.py` / public API

No surface change. `collect_container_coverage()` continues to work because `.coverage.container*` files and the `/tmp` extraction prefix are unchanged.

## Data Flow

### Inject (host-side, pre-container-start)

```
pytest startup
  → plugin.pytest_configure
    → load_config(pyproject.toml)            # PluginConfig + DriverConfig
    → resolver picks PythonDriver
    → PythonDriver.inject(build_dir, cfg)
        ├── if cfg.entrypoint is None: _inject_shim()
        │     ├── validate <build_dir>/run.sh exists + readable
        │     ├── if not <build_dir>/_orig_run.sh:
        │     │     shutil.copy2(run.sh → _orig_run.sh)
        │     ├── write .coveragerc
        │     ├── write _cov_wrapper.py     # subprocess _orig_run.sh
        │     ├── write run.sh              # exec python _cov_wrapper.py
        │     └── chmod +x run.sh, _orig_run.sh
        └── else: _inject_legacy()
              ├── write .coveragerc
              ├── write _cov_wrapper.py     # sh -c cfg.entrypoint
              ├── write run.sh
              └── chmod +x run.sh
```

### Runtime (inside container)

```
SAM local invokes /var/task/run.sh
  → exec python /var/task/_cov_wrapper.py
      ├── coverage.Coverage(.coveragerc) + cov.start()
      ├── signal.signal(SIGUSR1 + SIGTERM, _flush)
      ├── subprocess.Popen(["bash", "/var/task/_orig_run.sh"])
      │     ↳ user's original script runs verbatim
      ├── rc = proc.wait()
      ├── cov.stop() + cov.save()
      └── SystemExit(rc)
```

### Collect

Unchanged from today. `send_signal(SIGUSR1)` → in-container `_flush` → `cov.save()` → `extract_matching_files("/tmp", ".coverage.container*")`.

### Prod

```
Lambda invokes <build_dir>/run.sh from the deployed artifact (NOT injected).
  → exec python -m uvicorn app:create_app --factory ...  (or whatever the user's script says)
```

Plugin never touches the deployed artifact. Injection only happens in test sessions against a local `sam build` output.

## Error Handling

### Inject-time

| Condition | Behavior |
|-----------|----------|
| `<build_dir>` does not exist | `FileNotFoundError` w/ "Run `sam build` (or equivalent) before tests; expected build_dir at <path>." |
| `<build_dir>/run.sh` missing AND `cfg.entrypoint is None` | `RuntimeError` w/ migration hint pointing at the override field. |
| `<build_dir>/run.sh` unreadable | `PermissionError` propagates. |
| `_orig_run.sh` exists but `run.sh` is not the shim | Warning logged; both files rewritten (idempotent rewrite is safe). |
| `_orig_run.sh` does NOT exist but `run.sh` IS the shim (e.g. previous run used override path and overwrote `run.sh` without backup; now default path is in effect) | `RuntimeError` w/ "build_dir contains a shim `run.sh` but no `_orig_run.sh` to recover from; run `sam build` to reset". Prevents copying the shim onto itself as `_orig_run.sh`. |
| `shutil.copy2` fails mid-move | Propagate. Next run's sentinel check handles partial state. |
| `chmod +x` fails | Propagate. Silent failure here produces cryptic Lambda errors later. |
| `entrypoint = ""` in pyproject | `ValueError` at `load_config` time. |

### Runtime

| Condition | Behavior |
|-----------|----------|
| `_orig_run.sh` exits non-zero | `SystemExit(rc)` from wrapper. Coverage data is saved before exit. |
| SIGTERM received (Lambda timeout) | `_flush` runs (`cov.save()`); subprocess receives the signal too and dies; wrapper proceeds through second `cov.save()`. Belt-and-braces. |
| `coverage.Coverage()` init fails | Propagates → container exits → readiness check fails loudly. No silent uncovered run. |
| `_orig_run.sh` missing at runtime | `FileNotFoundError` → wrapper exits non-zero → LWA readiness fails → test surfaces the misconfiguration immediately. |

### Collect-time

Unchanged from today.

### Config validation

- `entrypoint = ""` → load-time error (above).
- `path_mapping` empty → warn at `load_config` (coverage reports won't translate container paths to host paths). Today's plugin silently accepts.

## Testing

### `tests/test_python_driver.py`

| Test | Coverage |
|------|----------|
| `test_inject_move_and_shim_default` | User `run.sh` present, no `entrypoint` config → `_orig_run.sh` byte-equal to source, +x preserved, `run.sh` is the shim, 4 files written. |
| `test_inject_idempotent_rerun` | `inject()` twice → `_orig_run.sh` unchanged from first run; other three artifacts overwritten cleanly. |
| `test_inject_override_path_skips_move` | `entrypoint="custom cmd"` → `_orig_run.sh` NOT created; wrapper contains `sh -c custom cmd`. |
| `test_inject_missing_run_sh_raises` | No `run.sh`, no override → `RuntimeError` w/ migration hint substring. |
| `test_inject_missing_run_sh_with_override_ok` | No `run.sh`, override set → success. |
| `test_inject_unreadable_run_sh_raises` | `chmod 000 run.sh` → `PermissionError`. |
| `test_inject_corrupt_state_warns_then_recovers` | `_orig_run.sh` exists but `run.sh` not the shim → warning + rewrite. |
| `test_inject_refuses_shim_run_sh_without_backup` | `run.sh` looks like the shim AND `_orig_run.sh` missing → `RuntimeError`. Prevents copying-shim-onto-self. |
| `test_cov_wrapper_template_renders_valid_python` | `ast.parse` both wrapper variants. |
| `test_run_sh_executable_bit_set` | `stat().st_mode & 0o111` for `run.sh` and `_orig_run.sh`. |

### `tests/test_config.py`

| Test | Coverage |
|------|----------|
| `test_load_config_entrypoint_omitted_yields_none` | Absent `entrypoint` → `DriverConfig.entrypoint is None`. |
| `test_load_config_entrypoint_empty_raises` | `entrypoint = ""` → `ValueError`. |
| `test_load_config_entrypoint_preserved_when_set` | `entrypoint = "x y z"` → preserved verbatim. |

### `tests/test_plugin.py` (existing)

Add one end-to-end test: temp build_dir with a real minimal `run.sh` invoking a 5-line Python module; call `inject()`; execute `bash <build_dir>/run.sh` directly w/ `COVERAGE_PROCESS_START` env set; assert `.coverage.container*` written and contains expected line hits. Validates the move-and-shim chain without a Docker dependency.

### `tests/test_docker_backend.py` (existing)

Add: build a tiny image with an injected build_dir; run briefly; collect; verify coverage extracted.

### Consumer-side regression (in chatbot-demo-2, separate change)

Plugin upgrade is gated on chatbot-demo-2's existing integration suite continuing to produce coverage output that combines cleanly with unit coverage. If the move-and-shim works, no test-code changes are required there.

## Migration

### Plugin repo

1. Implement changes above on branch `refactor/entrypoint-discovery`.
2. Bump version (minor — alpha allows; semver 0.x permits breaking changes in minor).
3. README update: rename the `entrypoint` row in the config table to "Override (discouraged): replace convention-discovered `run.sh` with this command via `sh -c`. Drift between this string and prod `run.sh` is the bug class this plugin's default path eliminates."
4. CHANGELOG: BREAKING entry with the migration path.
5. PyPI release.

### chatbot-demo-2 consumer (separate PR, post-plugin-release)

Delete:
- `src/api/_entrypoint.py`
- `tests/integration/conftest.py:_inject_coverage`, `_make_coveragerc`, `BUILD_DIR`, and the `COVERAGE_PROCESS_START` entry in `_write_test_env_json`'s `ApiFunction` env block.

Restore `src/api/run.sh` to a one-liner:

```bash
#!/bin/bash
exec python -m uvicorn app:create_app --factory --host 0.0.0.0 --port "${PORT:-8000}"
```

Add to `pyproject.toml`:

```toml
[tool.pytest-cov-container]
image_pattern = "samcli/lambda*"
label = "pytest-cov-container"

[tool.pytest-cov-container.path_mapping]
"src/api" = "/var/task"

[tool.pytest-cov-container.python]
build_dir = ".aws-sam/build/ApiFunction"
```

Replace `_collect_integration_coverage` body with a call to the plugin's public `collect_container_coverage()`.

Net consumer diff: ≈ −120 LOC, +12 LOC config.

## Open Questions

None at the time of writing. Override-path semantics are deliberately the legacy behavior to keep migration cheap for any unknown PyPI users.

## Alternatives Considered

- **Read-and-inline:** Plugin parses `<build_dir>/run.sh` and substitutes the command into the wrapper template. Rejected: shell parsing in plugin code is fragile (heredocs, conditionals, env substitution, line continuations). Loses byte-for-byte fidelity.
- **Sidecar entrypoint name:** Plugin writes wrapper to a different filename (e.g. `cov_run.sh`); user retargets their Lambda `Handler` to the new name in test-mode. Rejected: forks the bootstrap path between test and prod (the original bug class) and demands an environment-aware SAM template.
- **Validation hook on the string config:** Keep today's `entrypoint = "..."` API; add a `verify_against = "src/api/run.sh"` field that fails injection on diff. Rejected: structurally a workaround rather than a fix. Convention discovery eliminates the second declaration entirely.
