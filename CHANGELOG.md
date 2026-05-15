# Changelog

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
