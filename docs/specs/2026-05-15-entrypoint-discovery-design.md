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
2. Validate at least one `coverage*.pth` exists under `<build_dir>/**/site-packages/`. Raise `RuntimeError` with the migration hint otherwise. This is the gate for subprocess coverage attach.
3. If `<build_dir>/_orig_run.sh` does not exist, `shutil.copy2()` `run.sh` → `_orig_run.sh`. Mode preserved.
4. Always (idempotent): write `.coveragerc`, `_cov_wrapper.py`, `run.sh` (the shim). `chmod +x` on the two shell scripts.
5. Return `InjectionResult(files_written=[...], env_vars={"COVERAGE_PROCESS_START": "/var/task/.coveragerc"})`.

On re-injection (e.g. consecutive test runs without an intervening `sam build`), `_orig_run.sh`'s existence is the sentinel: the move step is skipped, the three config artifacts are refreshed. Idempotent.

If `_orig_run.sh` exists but `run.sh` does not look like the injected shim (a partial-failure recovery case), a warning is logged and both files are rewritten.

### Override Path (Legacy)

When `cfg.entrypoint` is a non-empty string:

1. No move. `_orig_run.sh` is not created by this path.
2. If `<build_dir>/_orig_run.sh` already exists (e.g. the build_dir was previously injected via the default path and the user has since added an `entrypoint` override), `Path.unlink()` it. The override wrapper does not reference `_orig_run.sh`, and leaving it in place would (a) confuse readers inspecting build_dir state and (b) defeat the default-path corruption detector (§5.1 "`_orig_run.sh` exists but `run.sh` is not the shim") on any future switch back to default. A debug-level log entry records the removal.
3. Write `.coveragerc`, `_cov_wrapper.py` (legacy `sh -c <entrypoint>` template), `run.sh` (shim exec'ing wrapper). `chmod +x` on `run.sh`.
4. Same `InjectionResult` shape.

Override path does NOT enforce the `coverage*.pth` precondition (its `sh -c` wrapper is a single Python process; subprocess attach is irrelevant).

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

# Propagate COVERAGE_PROCESS_START to child subprocess Python interpreters so
# subprocess coverage activates via coverage.process_startup() in each child.
# setdefault, not assignment: respect any caller-supplied override.
# NOTE: subprocess attach also requires a `coverage*.pth` file installed in
# the build_dir's site-packages — see Preconditions below.
os.environ.setdefault("COVERAGE_PROCESS_START", "/var/task/.coveragerc")

cov = coverage.Coverage(config_file=os.environ["COVERAGE_PROCESS_START"])
cov.start()

# `proc` must exist as a name before signal handlers are installed, because
# both handlers close over it. Pre-declare to None to make the closure safe
# even if a signal arrives in the window between handler install and Popen
# assignment (see "Handler installation order" below).
proc: subprocess.Popen | None = None


def _save(_signum, _frame):
    """SIGUSR1: host-side collect path. Save wrapper coverage only.

    Do NOT forward SIGUSR1 to the child: default Linux disposition for
    SIGUSR1 is `terminate`, and the user's app process (uvicorn, etc.) is
    unlikely to install a handler. Forwarding here would kill the running
    app on every collect call.
    """
    cov.save()


def _save_and_forward(signum, _frame):
    """SIGTERM: container shutdown. Save wrapper, then forward to child.

    Lambda delivers SIGTERM to the wrapper (PID 1) only — it does NOT
    propagate to the child subprocess automatically. Without forwarding,
    the child never sees the signal and the wrapper hangs in `proc.wait()`
    until the runtime hard-kills the container, truncating coverage.

    Installed AFTER cov.start(), so this overrides coverage's own SIGTERM
    handler (from `sigterm = true`) for the wrapper PID. Child Python
    procs still get `sigterm = true` semantics via their own subprocess
    coverage attach (see Preconditions).
    """
    cov.save()
    if proc is not None and proc.poll() is None:
        proc.send_signal(signum)


# Install handlers BEFORE Popen. Closing the race window where a signal
# during process spawn would fall through to Python's defaults (SIGTERM →
# terminate, no save, no forward).
signal.signal(signal.SIGUSR1, _save)
signal.signal(signal.SIGTERM, _save_and_forward)

proc = subprocess.Popen(["bash", "/var/task/_orig_run.sh"])
rc = proc.wait()
cov.stop()
cov.save()
raise SystemExit(rc)
```

#### Preconditions

The default-path wrapper relies on coverage's subprocess attach machinery to
measure the user's app (uvicorn workers, sub-shells that re-exec Python,
etc.). `COVERAGE_PROCESS_START` only triggers an attach in a child Python
interpreter if a `.pth` file in that interpreter's `site-packages` calls
`coverage.process_startup()` at import time.

`pip install coverage` ships such a `.pth` file by default
(`coverage*.pth`). The plugin therefore requires that the user's
`build_dir` contains `coverage` as an installed dependency.

`_inject_shim` validates this at inject time: if no `coverage*.pth` file is
found under any `*site-packages*` directory beneath `build_dir`, raise
`RuntimeError` with a hint: "build_dir at <path> has no installed
`coverage` package; subprocess coverage cannot attach. Add `coverage` to
the application's dependencies and re-run `sam build`." The override
(legacy) path does not enforce this (its `sh -c` wrapper is a single
Python process and does not rely on subprocess attach).

#### Differences from today

- `subprocess.Popen` invokes the moved script directly (not `sh -c <string>`).
- Wrapper explicitly owns SIGTERM (overrides coverage's `sigterm = true` handler for the wrapper PID) so it can forward the signal to the child. Child subprocess interpreters still get `sigterm = true` semantics via their own subprocess-attach.
- **SIGTERM is forwarded** to the child via `proc.send_signal(signum)` after the wrapper-level `cov.save()`. Without forwarding, the child never sees the signal and the wrapper hangs.
- **SIGUSR1 is NOT forwarded.** SIGUSR1 is the host→container collect signal. Forwarding would kill the child app (default disposition is `terminate`). Handlers are split: `_save` for SIGUSR1, `_save_and_forward` for SIGTERM.
- Handlers are installed **before** `Popen` (and the child closes over `proc`, which is pre-declared `None`). This closes a small race window where a signal during process spawn would otherwise hit Python's default disposition.
- `COVERAGE_PROCESS_START` is propagated into the wrapper's environment with `setdefault` so subprocess Python interpreters spawned by `_orig_run.sh` auto-attach coverage. The plugin no longer relies on the consumer's container env JSON to set this variable.
- Exit code propagated to caller (today the wrapper silently discards it).
- `CONTAINER_COV_ENTRYPOINT` env-var override is **removed** in the default path. It was a runtime escape for the pyproject string; obsolete now that the source-of-truth is `_orig_run.sh`. Legacy path retains it (see below).

Legacy `_cov_wrapper.py` template (override path) retains today's `sh -c` semantics and today's `CONTAINER_COV_ENTRYPOINT` env-var escape, with exit-code propagation, split SIGUSR1/SIGTERM handlers, and handler-before-Popen ordering added so behavior matches the default path on shutdown and collect.

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
        │     ├── validate <build_dir>/**/site-packages/coverage*.pth exists
        │     │     ↳ gate for subprocess coverage attach
        │     ├── refuse if run.sh is a shim AND _orig_run.sh missing
        │     ├── if not <build_dir>/_orig_run.sh:
        │     │     shutil.copy2(run.sh → _orig_run.sh)
        │     ├── write .coveragerc
        │     ├── write _cov_wrapper.py     # subprocess bash _orig_run.sh
        │     ├── write run.sh              # exec python _cov_wrapper.py
        │     └── chmod +x run.sh, _orig_run.sh
        └── else: _inject_legacy()
              ├── if <build_dir>/_orig_run.sh exists: unlink it
              │     ↳ avoid orphan from a prior default-path inject
              ├── write .coveragerc
              ├── write _cov_wrapper.py     # sh -c cfg.entrypoint
              ├── write run.sh
              └── chmod +x run.sh
```

### Runtime (inside container)

```
SAM local invokes /var/task/run.sh
  → exec python /var/task/_cov_wrapper.py
      ├── os.environ.setdefault(COVERAGE_PROCESS_START, /var/task/.coveragerc)
      │     ↳ inherited by all child Python interpreters
      │     ↳ coverage*.pth in build_dir site-packages calls
      │       coverage.process_startup() at child import time → subprocess attach
      ├── coverage.Coverage(.coveragerc) + cov.start()
      ├── proc = None (pre-declared for handler closure safety)
      ├── signal.signal(SIGUSR1, _save)                  # save only — collect
      ├── signal.signal(SIGTERM, _save_and_forward)      # save + forward — shutdown
      ├── proc = subprocess.Popen(["bash", "/var/task/_orig_run.sh"])
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
| No `coverage*.pth` found under any `*site-packages*` directory in `build_dir` AND `cfg.entrypoint is None` | `RuntimeError` w/ "build_dir at <path> has no installed `coverage` package; subprocess coverage cannot attach. Add `coverage` to the application's dependencies and re-run `sam build`." Override path is exempt. |
| `_orig_run.sh` exists but `run.sh` is not the shim | Warning logged; both files rewritten (idempotent rewrite is safe). |
| `_orig_run.sh` does NOT exist but `run.sh` IS the shim (e.g. previous run used override path and overwrote `run.sh` without backup; now default path is in effect) | `RuntimeError` w/ "build_dir contains a shim `run.sh` but no `_orig_run.sh` to recover from; run `sam build` to reset". Prevents copying the shim onto itself as `_orig_run.sh`. |
| `shutil.copy2` fails mid-move | Propagate. Next run's sentinel check handles partial state. |
| `chmod +x` fails | Propagate. Silent failure here produces cryptic Lambda errors later. |
| `entrypoint = ""` in pyproject | `ValueError` at `load_config` time. |

### Runtime

| Condition | Behavior |
|-----------|----------|
| `_orig_run.sh` exits non-zero | `SystemExit(rc)` from wrapper. Coverage data is saved before exit. |
| SIGUSR1 received (host-initiated collect) | `_save` runs in wrapper: `cov.save()`. Signal is NOT forwarded to the child — the user's app keeps running. Host then extracts `.coverage.container*` from `/tmp`. |
| SIGTERM received (Lambda timeout / container stop) | `_save_and_forward` runs in wrapper: `cov.save()` then `proc.send_signal(SIGTERM)` to the child. Child runs its own coverage SIGTERM handler (via `sigterm = true` in `.coveragerc`, attached through `COVERAGE_PROCESS_START` + `coverage*.pth`) and exits. Wrapper's `proc.wait()` returns child's rc; final `cov.stop()` + `cov.save()` runs; `SystemExit(rc)`. Lambda's ~500ms grace before SIGKILL must accommodate both saves and the child's shutdown — keep handlers cheap. |
| `coverage.Coverage()` init fails | Propagates → container exits → readiness check fails loudly. No silent uncovered run. |
| `_orig_run.sh` missing at runtime | `FileNotFoundError` → wrapper exits non-zero → LWA readiness fails → test surfaces the misconfiguration immediately. |
| Signal arrives between handler install and `proc =` assignment (microsecond window) | Handler fires with `proc is None`; only wrapper-level `cov.save()` runs (forward branch short-circuits). Control returns to the main thread; `Popen` completes; `proc` is assigned. Wrapper enters `proc.wait()` with no signal forwarded — Python has already swallowed the original signal via the installed handler, so default disposition does not apply. Child then runs until it exits naturally OR the runtime hard-kills the container (Lambda: SIGKILL after the ~500ms grace following SIGTERM). Result: saved-but-orphaned wrapper-side coverage snapshot, child-side coverage truncated. Acceptable: the race window is microseconds and the failure mode is "less coverage than ideal," not "hang" or "kill." |

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
| `test_inject_override_path_unlinks_stale_orig_run_sh` | Pre-create `<build_dir>/_orig_run.sh` (simulating a prior default-path inject). Run inject with `entrypoint="custom cmd"`. Assert `_orig_run.sh` no longer exists, override wrapper written normally, no exception. Guards default→override switch leaving orphans. |
| `test_inject_missing_run_sh_raises` | No `run.sh`, no override → `RuntimeError` w/ migration hint substring. |
| `test_inject_missing_run_sh_with_override_ok` | No `run.sh`, override set → success. |
| `test_inject_unreadable_run_sh_raises` | `chmod 000 run.sh` → `PermissionError`. |
| `test_inject_corrupt_state_warns_then_recovers` | `_orig_run.sh` exists but `run.sh` not the shim → warning + rewrite. |
| `test_inject_refuses_shim_run_sh_without_backup` | `run.sh` looks like the shim AND `_orig_run.sh` missing → `RuntimeError`. Prevents copying-shim-onto-self. |
| `test_cov_wrapper_template_renders_valid_python` | `ast.parse` both wrapper variants. |
| `test_run_sh_executable_bit_set` | `stat().st_mode & 0o111` for `run.sh` and `_orig_run.sh`. |
| `test_cov_wrapper_forwards_sigterm_to_child` | Spawn rendered wrapper invoking a short-lived shell child that traps SIGTERM and writes a sentinel file. `kill -TERM` the wrapper PID. Assert sentinel exists (child observed signal), `.coverage*` file present (wrapper saved), wrapper exit code matches child rc. Without forwarding, child would not write sentinel. |
| `test_cov_wrapper_sigusr1_saves_without_killing_child` | Spawn rendered wrapper with child `sleep 30`. `kill -USR1` the wrapper PID. Assert `.coverage*` exists AND child PID still running. Then `kill -TERM` and assert child observed it. Guards against SIGUSR1-forwarding regression (would kill the running app on every collect). |
| `test_cov_wrapper_propagates_coverage_env_to_child` | Spawn rendered wrapper; child script prints `COVERAGE_PROCESS_START` from its env. Assert child saw `/var/task/.coveragerc` (or test-overridden path). Guards regression in `os.environ.setdefault` line. |
| `test_cov_wrapper_subprocess_coverage_attaches` | Spawn rendered wrapper whose child invokes a small Python script with a known line range. After collect (`kill -USR1` + extract), assert `.coverage.container*` contains line hits from the CHILD script, not just the wrapper. Without a `coverage*.pth` present this fails — exactly the silent-empty case the inject-time precondition check is designed to prevent. |
| `test_inject_missing_coverage_pth_raises` | `build_dir` with `run.sh` but no `coverage*.pth` under any `site-packages` → `RuntimeError` with the migration hint substring. Override path with same build_dir: success (legacy path exempt). |

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

## Resolved Design Decisions

Recorded so future readers see *why* the wrapper looks the way it does.

- **Wrapper owns SIGTERM, not coverage.** `signal.signal(SIGTERM, _save_and_forward)` is installed *after* `cov.start()`, which replaces the SIGTERM handler that `sigterm = true` installs. Reason: only the wrapper can both save its own coverage AND forward the signal to the child subprocess (Lambda delivers SIGTERM to PID 1 only — it does not propagate to children). Child Python procs still get `sigterm = true` behavior because they attach coverage independently via `coverage.process_startup()` triggered by `COVERAGE_PROCESS_START` + the `coverage*.pth` file installed in their site-packages.
- **`COVERAGE_PROCESS_START` propagation is the wrapper's job.** Setting it via `os.environ.setdefault` inside `_cov_wrapper.py` (rather than relying on the consumer's SAM env JSON) means subprocess coverage works for any consumer regardless of whether they wire env vars in their SAM template. The `InjectionResult.env_vars` return remains for callers that still want to set the var at container-launch time, but it is no longer load-bearing.
- **SIGTERM forwarded, SIGUSR1 NOT forwarded.** SIGTERM means "container is shutting down"; the child must die for `proc.wait()` to return. SIGUSR1 means "host wants a coverage flush"; forwarding would terminate the child app on every collect call (Linux default disposition for SIGUSR1 is `terminate`, and user apps rarely install a handler). Two handlers, one role each: `_save` (SIGUSR1) and `_save_and_forward` (SIGTERM).
- **Handlers installed before `Popen`, `proc` pre-declared `None`.** Closes the race where a signal during process spawn would otherwise hit Python's default disposition. Forward branch in `_save_and_forward` is guarded by `proc is not None and proc.poll() is None`, so an in-flight SIGTERM during spawn produces a saved-but-orphaned snapshot rather than a wrapper crash.
- **Subprocess coverage attach requires a `coverage*.pth` in build_dir site-packages.** Setting `COVERAGE_PROCESS_START` alone is not enough — coverage's subprocess attach is triggered by a `.pth` file (shipped by `pip install coverage`) that calls `coverage.process_startup()` at child import time. Default-path inject validates this precondition and fails loudly if the build_dir is missing the package. Without this check, the default path silently produces coverage data containing only the wrapper PID's (uninteresting) line hits.
- **Override path unlinks any pre-existing `_orig_run.sh`.** A build_dir previously injected via the default path leaves `_orig_run.sh` behind. If the user then adds an `entrypoint = "..."` override, the override wrapper does not reference it, but leaving it in place breaks the default-path corruption detector on any future switch back. Override path therefore actively removes the file (no-op if absent) and logs at debug level. The default→override→default switch is fully reversible without manual cleanup.

## Alternatives Considered

- **Read-and-inline:** Plugin parses `<build_dir>/run.sh` and substitutes the command into the wrapper template. Rejected: shell parsing in plugin code is fragile (heredocs, conditionals, env substitution, line continuations). Loses byte-for-byte fidelity.
- **Sidecar entrypoint name:** Plugin writes wrapper to a different filename (e.g. `cov_run.sh`); user retargets their Lambda `Handler` to the new name in test-mode. Rejected: forks the bootstrap path between test and prod (the original bug class) and demands an environment-aware SAM template.
- **Validation hook on the string config:** Keep today's `entrypoint = "..."` API; add a `verify_against = "src/api/run.sh"` field that fails injection on diff. Rejected: structurally a workaround rather than a fix. Convention discovery eliminates the second declaration entirely.
