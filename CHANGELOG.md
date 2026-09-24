# Changelog

## 0.4.1 — 2026-09-24

First PyPI release of the 0.4.0 changes below. The 0.4.0 tag exists but was
never published: its release workflow failed.

### Fixed

- **Release workflow**: the tag/version check imported
  `src.pytest_cov_container.__about__`, which runs the package `__init__.py`
  before the package is installed (`ModuleNotFoundError`). It now reads
  `__about__.py` with `runpy.run_path`.

## 0.4.0 — 2026-09-24 (not published)

Makes the plugin usable with `sam local` under pytest-xdist and with several
checkouts of one project running at once. Driven by a SAM project that had
hand-rolled all of this in its test fixtures.

### Fixed

- **Plugin never activated**: `pytest_configure` registered the plugin
  instance under `cov_container`, the name its own pytest11 entry-point
  module already holds, so every real activation raised
  `ValueError: Plugin name already registered` (INTERNALERROR). The instance
  now registers as `cov_container_session`.
- **Host coverage clobbered**: the end-of-session `coverage combine` ran
  without `--append`, after pytest-cov had written its data, so it replaced
  the host data with container data. Collected files now become suffix files
  of pytest-cov's data file (`<data_file>.container-<worker>-<id>`) and
  pytest-cov's own combine merges them, so its report and `--cov-fail-under`
  include container coverage.
- **Collection came too late for the report**: collection moved from
  `pytest_sessionfinish` (after pytest-cov reported) to a `pytest_runtestloop`
  wrapper that runs before pytest-cov's.
- **Project coverage config ignored**: combine used a `[paths]`-only rcfile.
  It now runs under the project's config (`[paths]`, `branch`, `omit`);
  `path_mapping` still applies, now when each file is handed over.
- **Branch/statement mismatch**: the injected `.coveragerc` now copies the
  host's `branch` setting (overridable with `[python] branch`). Before, a
  project with `branch = true` failed to combine with "Can't combine branch
  coverage data with statement data".
- **Self-signalling `/proc` scan**: the `sh -c` that scanned
  `/proc/*/cmdline` for `_cov_wrapper` carried that token in its own command
  line, could match itself, and SIGUSR1's default action killed it before it
  reported. Signalling now uses a pid file (see "Save protocol").
- **Truncated copies**: completion was detected by polling the data file's
  mtime (2 s cap), which can fire mid-write. The coverage process now writes a
  sentinel after `cov.save()` returns and the host waits for it (10 s cap).
- **SQLite side files** (`-journal`, `-wal`, `-shm`) are no longer extracted
  as data files. Files from different containers can no longer overwrite each
  other in the staging directory.
- **False "No data was collected" warning**: a process whose tests only drive
  containers measures nothing itself, so coverage warned at save time even
  though container data was about to be combined. Once a container yields
  data, the plugin silences that warning for the process. A process that gets
  nothing from its containers still warns.

### Added

- **Ownership keys** for tools that set no Docker labels (`sam local`):
  `mount_prefix` (the container bind-mounts a directory under
  `<rootdir>/<mount_prefix>/`; nested checkouts and prefix-sharing siblings do
  not match) and `worker_env` (the container env carries
  `<worker_env>=<xdist worker id>`).
- **xdist support**: the controller injects before workers start; each worker
  collects only its own containers; the controller does not collect.
- `required = true` / `--cov-container-required`: a collection pass that gets
  no data raises (explicit call) or fails the session (end-of-session pass).
- `[python] wrapper = false`: the application starts coverage itself and runs
  the save protocol; the plugin writes only `.coveragerc` and leaves `run.sh`
  alone.
- `[python] source_dir` (measure only this host directory's `*.py`, rendered
  under `container_root`), `container_root` (default `/var/task`),
  `relative_files` (default `true`), `branch`.
- `image_pattern` accepts a list. Matching uses the container's
  `Config.Image` first (no image lookup), then its tags.
- Public API: `container_env(rootpath)`, `owned_containers(rootpath,
  all_workers=False)`, `worker_id()`, `PID_FILE`, `DONE_FILE`.
  `collect_container_coverage()` returns the number of files collected.

### Changed

- **Minimum dependency versions raised** to the latest releases, all of which
  support Python 3.11: `pytest>=9.1.1`, `pytest-cov>=7.1.0`, `docker>=7.2.0`,
  `coverage>=7.16.1`. The old floors were broken: the plugin needs pluggy's
  new-style hook wrappers (pluggy 1.2+, which pytest 7.0 did not require) and
  `CoverageData.close()` (coverage 7.10.3+).
- **SAM defaults**: `label` defaults to `sam.cli.container.type=lambda`, which
  SAM CLI 1.165+ sets on every Lambda container, and `mount_prefix` defaults
  to the parent of `build_dir` (the SAM build root, so only this checkout's
  containers match). A config with no selector used to match every container
  on the host. `""` switches either filter off. The "no coverage collected"
  error now names the label filter.
- Injected files are written atomically (temp file + rename).
- The docker client is created on first use, not at configure time.
- `DockerBackend.file_signature` / `wait_for_save` are replaced by
  `wait_for_done`. `LanguageDriver.inject` takes `rootpath` and `branch`
  keywords, `collect` returns the extracted files, and drivers implement
  `container_env(config)`.
- `scripts/release.py` bumps the minor version when called with no argument
  (was: release the current `__about__.py` version as-is). `--dry-run` now
  predicts `major`/`minor`/`patch` bumps, so it checks the new tag instead of
  the current one.

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
