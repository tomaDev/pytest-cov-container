"""Cut a release.

Usage:
    hatch run release [VERSION_OR_SEGMENT]
    python scripts/release.py [VERSION_OR_SEGMENT]

Examples:
    hatch run release           # use current __about__.py version
    hatch run release patch     # 0.2.0 → 0.2.1
    hatch run release minor     # 0.2.0 → 0.3.0
    hatch run release major     # 0.2.0 → 1.0.0
    hatch run release 0.3.5     # explicit version

If an argument is given, it is passed straight to `hatch version`, which
accepts either a literal version or a segment keyword (`patch`, `minor`,
`major`, `rc`, `b`, `a`, `post`, `dev`, etc.). The resolved version is
then read back and used for the release commit + tag.

Workflow:
    1. Verify clean working tree on main (BEFORE any mutation).
    2. If an argument is given: bump __about__.py, commit "release X.Y.Z".
    3. Verify the resolved tag does not already exist locally or on origin.
    4. Push main.
    5. Create and push the tag.

Pushing the tag triggers .github/workflows/release.yaml, which builds and
publishes to PyPI via OIDC trusted publisher.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ABOUT = ROOT / "src" / "pytest_cov_container" / "__about__.py"


def _run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(args, check=True, capture_output=capture, text=True)
    return (result.stdout or "").strip() if capture else ""


def _read_version() -> str:
    match = re.search(r'__version__\s*=\s*"([^"]+)"', ABOUT.read_text())
    if match is None:
        raise SystemExit("error: could not parse __version__ from __about__.py")
    return match.group(1)


def _bump_and_commit(spec: str) -> None:
    """Pass `spec` to `hatch version` (literal X.Y.Z or segment like `patch`),
    then read the resolved version back and commit if it changed."""
    current = _read_version()
    _run("hatch", "version", spec)
    new = _read_version()
    if new == current:
        print(f"version already at {current}; skipping bump")
        return
    _run("git", "add", str(ABOUT.relative_to(ROOT)))
    _run("git", "commit", "-m", f"release {new}")
    print(f"bumped {current} → {new}")


def main() -> int:
    if len(sys.argv) > 2:
        print("usage: release.py [VERSION]", file=sys.stderr)
        return 2

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

    if len(sys.argv) == 2:
        _bump_and_commit(sys.argv[1])

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
    _run("git", "push", "origin", "main")
    _run("git", "tag", version)
    _run("git", "push", "origin", version)
    print(f"pushed tag {version} — release workflow will pick it up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
