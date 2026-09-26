"""Cut a release.

Usage:
    hatch run release [VERSION_OR_SEGMENT] [--dry-run]
    python scripts/release.py [VERSION_OR_SEGMENT] [--dry-run]

Examples:
    hatch run release                     # 0.2.0 → 0.2.1 (default: patch)
    hatch run release patch               # 0.2.0 → 0.2.1
    hatch run release minor               # 0.2.0 → 0.3.0
    hatch run release major               # 0.2.0 → 1.0.0
    hatch run release 0.3.5               # explicit version
    hatch run release patch --dry-run     # show plan, mutate nothing

The argument (default `patch`) is passed straight to `hatch version`, which
accepts either a literal version or a segment keyword (`patch`, `minor`,
`major`, `rc`, `b`, `a`, `post`, `dev`, etc.). The resolved version is
then read back and used for the release commit + tag.

Workflow:
    1. Verify clean working tree on main (BEFORE any mutation).
    2. `git fetch origin main`, then fast-forward local main if behind.
       Refuses if origin/main has diverged.
    3. Run the e2e suite against real `sam local` containers on this
       machine (Docker Desktop, which CI cannot run); refuse on failure.
       The unit matrix runs in CI (release.yaml) before anything publishes.
    4. Bump __about__.py, commit "release X.Y.Z" (every pre-commit hook runs).
    5. Verify the resolved tag does not already exist locally or on origin.
    6. Push main.
    7. Create and push the tag.

`--dry-run` skips every mutating step (merge, bump, commit, push, tag) and
prints what would have run. Read-only checks (branch, status, fetch, tag
existence) still execute so the plan reflects the real repository state.

Pushing the tag triggers .github/workflows/release.yaml, which builds and
publishes to PyPI via OIDC trusted publisher.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ABOUT = ROOT / "src" / "pytest_cov_container" / "__about__.py"
LITERAL_VERSION = r"\d+\.\d+\.\d+([.+-].*)?"
SEGMENTS = ("major", "minor", "patch")


def _run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, check=True, capture_output=capture, text=True)
    return (result.stdout or "").strip() if capture else ""


def _mutate(*args: str, dry_run: bool) -> None:
    """Run a mutating command, OR print it under --dry-run."""
    if dry_run:
        print("[dry-run] " + " ".join(args))
        return
    _run(*args)


def _read_version() -> str:
    match = re.search(r'__version__\s*=\s*"([^"]+)"', ABOUT.read_text())
    if match is None:
        raise SystemExit("error: could not parse __version__ from __about__.py")
    return match.group(1)


def _preview_version(spec: str, current: str) -> str | None:
    """Predict what `hatch version <spec>` resolves to, without running it.
    Handles literals and the major/minor/patch segments of an X.Y.Z version;
    returns None for anything else (rc, dev, ...)."""
    if re.fullmatch(LITERAL_VERSION, spec):
        return spec
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", current)
    if spec not in SEGMENTS or match is None:
        return None
    parts = [int(part) for part in match.groups()]
    index = SEGMENTS.index(spec)
    parts[index] += 1
    parts[index + 1 :] = [0] * (len(parts) - index - 1)
    return ".".join(map(str, parts))


def _bump_and_commit(spec: str, *, dry_run: bool) -> str | None:
    """Pass `spec` to `hatch version`, then read the resolved version back
    and commit if it changed. Returns the resolved version (or current, on
    no-op).

    Under --dry-run: predict the resolved version via `_preview_version`,
    never write the file or commit. Returns None if it cannot be predicted."""
    current = _read_version()
    if dry_run:
        # `hatch version <spec>` always writes __about__.py; it has no
        # preview flag.
        new = _preview_version(spec, current)
        shown = new or f"<resolved by `hatch version {spec}` at run time>"
        print(f"[dry-run] hatch version {spec}  (would bump {current} → {shown})")
        print(f"[dry-run] git add {ABOUT.relative_to(ROOT)}")
        print(f"[dry-run] git commit -m 'release {shown}'")
        return new

    _run("hatch", "version", spec)
    new = _read_version()
    if new == current:
        print(f"version already at {current}; skipping bump")
        return current
    _run("git", "add", str(ABOUT.relative_to(ROOT)))
    _run("git", "commit", "-m", f"release {new}")
    print(f"bumped {current} → {new}")
    return new


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="release",
        description="Cut a release: bump (optional), commit, push, tag.",
    )
    parser.add_argument(
        "version",
        nargs="?",
        default="patch",
        help="Literal version (0.3.5) or hatch segment (patch/minor/major/rc/...). "
        "Default: patch.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned mutations without executing them.",
    )
    args = parser.parse_args(argv)
    dry_run = args.dry_run

    if dry_run:
        print("=== DRY RUN — no mutations will be made ===")

    # Preflight BEFORE any mutation, so a dirty tree never produces a
    # "release X.Y.Z" commit that then fails to push.
    branch = _run("git", "branch", "--show-current", capture=True)
    if branch != "main":
        print(f"error: must be on main (currently {branch!r})", file=sys.stderr)
        return 1

    status = _run("git", "status", "--porcelain", capture=True)
    if status:
        print("error: working tree not clean:\n" + status, file=sys.stderr)
        return 1

    # Sync with origin BEFORE bumping so a divergent remote does not produce
    # a stranded "release X.Y.Z" commit that can never be pushed.
    # Fetch itself is read-only (only updates remote-tracking ref); run in
    # dry-run too so behind/ahead numbers reflect reality.
    print("fetching origin/main")
    _run("git", "fetch", "origin", "main")
    behind = _run("git", "rev-list", "--count", "main..origin/main", capture=True)
    ahead = _run("git", "rev-list", "--count", "origin/main..main", capture=True)
    if int(behind) > 0 and int(ahead) > 0:
        print(
            f"error: main has diverged from origin/main "
            f"({ahead} ahead, {behind} behind). Rebase or merge before releasing.",
            file=sys.stderr,
        )
        return 1
    if int(behind) > 0:
        print(f"fast-forwarding {behind} commit(s) from origin/main")
        _mutate("git", "merge", "--ff-only", "origin/main", dry_run=dry_run)

    # Real `sam local` containers on this machine, BEFORE the bump: a failure
    # leaves nothing to undo. CI covers the Linux engine and the unit matrix;
    # only here does the Docker Desktop path run. Fails without Docker or SAM
    # CLI. Serial (-n 0): concurrent runs would race SAM's image builds.
    # Read-only, so it runs under --dry-run too.
    print("running the e2e suite (hatch test -m e2e)")
    try:
        _run("hatch", "test", "-m", "e2e", "-n", "0")
    except subprocess.CalledProcessError:
        print("error: tests failed; nothing was bumped", file=sys.stderr)
        return 1

    # Under dry-run, check the predicted version against the tag space; fall
    # back to the current __about__.py when it cannot be predicted (rc, dev).
    version = _bump_and_commit(args.version, dry_run=dry_run) or _read_version()

    local_tag = _run("git", "tag", "--list", version, capture=True)
    if local_tag:
        print(f"error: tag {version} already exists locally", file=sys.stderr)
        return 1

    remote_tag = _run(
        "git", "ls-remote", "--tags", "origin", f"refs/tags/{version}", capture=True
    )
    if remote_tag:
        print(f"error: tag {version} already exists on origin", file=sys.stderr)
        return 1

    print(f"cutting release {version}")
    _mutate("git", "push", "origin", "main", dry_run=dry_run)
    _mutate("git", "tag", version, dry_run=dry_run)
    _mutate("git", "push", "origin", version, dry_run=dry_run)
    if dry_run:
        print(f"[dry-run] would publish {version}; nothing was actually pushed.")
    else:
        print(f"pushed tag {version} — release workflow will pick it up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
