"""Cut a release.

Usage:
    hatch run release [VERSION_OR_SEGMENT] [--dry-run]
    python scripts/release.py [VERSION_OR_SEGMENT] [--dry-run]

Examples:
    hatch run release                     # release current __about__.py version
    hatch run release patch               # 0.2.0 → 0.2.1
    hatch run release minor               # 0.2.0 → 0.3.0
    hatch run release major               # 0.2.0 → 1.0.0
    hatch run release 0.3.5               # explicit version
    hatch run release patch --dry-run     # show plan, mutate nothing

If an argument is given, it is passed straight to `hatch version`, which
accepts either a literal version or a segment keyword (`patch`, `minor`,
`major`, `rc`, `b`, `a`, `post`, `dev`, etc.). The resolved version is
then read back and used for the release commit + tag.

Workflow:
    1. Verify clean working tree on main (BEFORE any mutation).
    2. `git fetch origin main`, then fast-forward local main if behind.
       Refuses if origin/main has diverged.
    3. If an argument is given: bump __about__.py, commit "release X.Y.Z".
    4. Verify the resolved tag does not already exist locally or on origin.
    5. Push main.
    6. Create and push the tag.

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


def _bump_and_commit(spec: str, *, dry_run: bool) -> str:
    """Pass `spec` to `hatch version`, then read the resolved version back
    and commit if it changed. Returns the resolved version (or current, on
    no-op).

    Under --dry-run: ask `hatch version` what the segment would resolve to
    via `hatch version --dry-run`, but never write the file or commit."""
    current = _read_version()
    if dry_run:
        # `hatch version <spec>` mutates by default; preview-only flag varies
        # by hatch version. Fall back to printing intent and assuming the
        # spec is the resolved version when it looks like a literal.
        if re.fullmatch(r"\d+\.\d+\.\d+([.+-].*)?", spec):
            new = spec
        else:
            new = f"<resolved by `hatch version {spec}` at run time>"
        print(f"[dry-run] hatch version {spec}  (would bump {current} → {new})")
        print(f"[dry-run] git add {ABOUT.relative_to(ROOT)}")
        print(f"[dry-run] git commit -m 'release {new}'")
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
        help="Literal version (0.3.5) or hatch segment (patch/minor/major/rc/...).",
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

    if args.version is not None:
        _bump_and_commit(args.version, dry_run=dry_run)

    # In dry-run with a literal new version (e.g. 0.3.5 or a resolved literal
    # from a segment), check that intended version against the tag space.
    # Otherwise fall back to the current __about__.py (no-arg case or
    # unresolved segment under dry-run).
    if dry_run and args.version is not None and re.fullmatch(
        r"\d+\.\d+\.\d+([.+-].*)?", args.version
    ):
        version = args.version
    else:
        version = _read_version()

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
