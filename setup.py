from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

ROOT = Path(__file__).resolve().parent


def _git_output(*argv: str) -> str | None:
    try:
        completed = subprocess.run(
            ("git", "-C", str(ROOT), *argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


class BuildPy(_build_py):
    """Write immutable provenance into the built package, never the source tree."""

    def run(self) -> None:
        super().run()
        git_sha = _git_output("rev-parse", "HEAD")
        status = _git_output("status", "--porcelain", "--untracked-files=normal", "--", ".")
        clean_tree = git_sha is not None and status == ""
        built_at = datetime.now(timezone.utc).isoformat()
        target = Path(self.build_lib) / "hermes_codex_router" / "_build_info.py"
        target.write_text(
            "\n".join(
                (
                    '"""Generated release identity. Do not edit."""',
                    "",
                    f"PACKAGE_VERSION = {self.distribution.get_version()!r}",
                    f"GIT_SHA = {git_sha!r}",
                    f"BUILT_AT = {built_at!r}",
                    f"CLEAN_TREE = {clean_tree!r}",
                    "",
                )
            ),
            encoding="utf-8",
        )


setup(cmdclass={"build_py": BuildPy})
