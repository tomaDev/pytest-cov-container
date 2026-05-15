# Changelog

## 0.3.0 — 2026-05-15

Security + correctness + performance hardening pass driven by a multi-agent
code review (`comprehensive-review:full-review`). 10 Critical and 1 High
finding addressed. No public-API breakage.

### Security

- **CRITICAL** (CVSS 8.1, CWE-22 / CWE-59): tarfile extraction now applies
  PEP 706 `data_filter` (3.12+) or hand-rolled equivalent (3.11) when
  extracting coverage data from container images. Defense-in-depth against
  path-traversal / symlink escape from a compromised container image.
- **HIGH** (CWE-94 + CWE-78): legacy-path `entrypoint` is no longer
  interpolated into the wrapper's Python source via `str.format()`.
  Instead, the entrypoint is written to a sidecar `_cov_entrypoint.json`
  and loaded by the wrapper at runtime. Eliminates a two-layer code
  injection that previously gave RCE on the developer's machine if a
  hostile pyproject.toml planted Python syntax in the `entrypoint` string.

### Fixed

- **Coverage data loss**: `_combine_coverage` failures now `raise
  RuntimeError` instead of emitting a swallowed `UserWarning`. Previously
  CI would exit green even though no coverage data merged.
- **Missed save signals**: `send_signal`'s shell loop no longer `break`s
  after the first match, so multi-process containers (uvicorn workers,
  gunicorn) flush all wrappers. The loop now emits `signalled=N`, which
  the host parses and warns on `signalled=0` instead of silently
  extracting an empty tar.
- **Race in collect**: `PythonDriver.collect` no longer blanket-sleeps for
  1 second after signalling. Replaced with a 50 ms-interval, 2 s-cap poll
  on the coverage-data-file mtime via two new `DockerBackend` methods
  (`file_signature`, `wait_for_save`). Real save completes in 1-20 ms, so
  this returns ~50× faster on the common path and is bounded on slow
  hosts.
- **`/var/task` no longer hardcoded** in wrapper artifacts. The wrapper
  self-locates via `os.path.dirname(__file__)` and the shim `run.sh` uses
  `dirname "$0"`. Removes the Lambda-only assumption; non-SAM deployments
  that mount the build_dir elsewhere now work.

### Changed

- **Concurrent collect**: session-finish now fans out `driver.collect`
  across a `ThreadPoolExecutor` (max_workers=8) instead of running
  containers sequentially. 20-container teardown drops from ~28 s to ~2 s
  on a remote docker daemon. Per-container failures are warned and the
  others continue.
- **Bounded `.pth` discovery**: `_find_coverage_pth` now uses direct
  probes (flat / `python{X.Y}/site-packages/` / one-level fallback)
  instead of `rglob("site-packages")` walking the entire build_dir tree.
  200-800 ms → <1 ms on real SAM builds (60-120k files).
- **`_active_plugin` singleton**: registration now happens via the public
  `_register_active_plugin()` helper rather than direct attribute
  assignment with `# noqa: SLF001`. Documents the standard pytest plugin
  idiom and makes the call site type-checkable.

### Internal

- New `DockerBackend.file_signature(container_id, source_dir, prefix)` and
  `DockerBackend.wait_for_save(container_id, source_dir, prefix, baseline,
  timeout=2.0, interval=0.05)` methods. `DockerBackendProtocol` extended
  to match.
- Test count: 59 → 82. New coverage for tarfile traversal/symlink
  rejection, signal-count parsing, save-poll, sidecar JSON, multi-container
  fanout, combine-failure-raises, parameterized wrapper paths.

## 0.2.0 — 2026-05-15

### BREAKING

- **`entrypoint` is now optional and repurposed as an override.** The Python driver discovers the user's entrypoint from `<build_dir>/run.sh` by convention (move-and-shim). The previous string-based `entrypoint = "..."` declaration in `pyproject.toml` is no longer required and is now treated as a discouraged override of the convention-discovered script.
  - `entrypoint` absent (recommended) → convention discovery → plugin moves `run.sh` to `_orig_run.sh` and shims `run.sh` to exec a coverage wrapper that runs `_orig_run.sh`. Production entrypoint is the test entrypoint by construction; no command-string duplication.
  - `entrypoint` set to a non-empty string → legacy behavior (today's `sh -c <entrypoint>` wrapper).
  - `entrypoint = ""` → `ValueError` at config load time.
  - Default-path inject now validates that `<build_dir>/**/site-packages/coverage*.pth` exists. If not, raises `RuntimeError`. This is the gate for subprocess coverage attach in the user's app processes (e.g. uvicorn workers).

### Fixed

- Wrapper now propagates child exit code via `SystemExit(rc)` instead of silently discarding it.
- Wrapper now forwards `SIGTERM` to the child subprocess before waiting on it. Previously, the wrapper hung in `proc.wait()` until the container was hard-killed by the runtime, truncating coverage data.
- `SIGUSR1` handler now saves coverage **without** forwarding the signal to the child. Forwarding would have terminated the user's app on every `collect_container_coverage()` call (Linux default disposition for SIGUSR1 is `terminate`).
- Signal handlers are installed before `subprocess.Popen` is called; the race window where a signal during process spawn would hit Python's default disposition is closed.

### Migration

If your previous `pyproject.toml` had:

```toml
[tool.pytest-cov-container.python]
build_dir = ".aws-sam/build/ApiFunction"
entrypoint = "uvicorn app:app --host 0.0.0.0 --port 8080"
```

Remove the `entrypoint` line. Ensure your build_dir's `run.sh` (produced by `sam build`) invokes your real entrypoint, and that `coverage` is listed in your application's runtime dependencies so it is installed into the build_dir's `site-packages`. No further consumer changes are required.

If you need test-mode to differ from production (e.g. fixture-injected port, mocked args), keep the `entrypoint = "..."` field. Drift between this string and your prod `run.sh` becomes your responsibility.

## 0.0.1

- Initial PyPI release.
