#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*argv: str) -> None:
    subprocess.run(argv, cwd=ROOT, check=True)


def main() -> int:
    run("ruff", "format", "--check", ".")
    run("ruff", "check", ".")
    run("pyright")
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q")
    run(
        sys.executable,
        "-m",
        "hermes_codex_router.cli",
        "validate",
        "config/projects.example.json",
        "--allow-missing",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
