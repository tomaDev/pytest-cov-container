"""Cut a release.

Reads __version__ from src/pytest_cov_container/__about__.py, then:
  1. Verifies clean working tree on main.
  2. Verifies the tag does not already exist locally or on origin.
  3. Pushes main.
  4. Creates and pushes the tag.

Pushing the tag triggers .github/workflows/release.yaml, which builds and
publishes to PyPI via OIDC trusted publisher.

Run via: `hatch run release:cut` (or `python scripts/cut_release.py`).
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


def main() -> int:
    version = _read_version()

    branch = _run("git", "branch", "--show-current", capture=True)
    if branch != "main":
        print(f"error: must be on main (currently {branch!r})", file=sys.stderr)
        return 1

    status = _run("git", "status", "--porcelain", capture=True)
    if status:
        print("error: working tree not clean:\n" + status, file=sys.stderr)
        return 1

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
