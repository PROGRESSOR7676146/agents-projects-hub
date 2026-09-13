from __future__ import annotations

import subprocess
from pathlib import Path


def init_git_root(root: Path) -> None:
    """A real, empty fictional repository; no commit, hooks or network."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ("git", "init", "--quiet", "--template=", str(root)),
        check=True,
        capture_output=True,
        timeout=5,
    )
