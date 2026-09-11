#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def tool(name: str) -> str:
    sibling = Path(sys.executable).with_name(name)
    return str(sibling) if sibling.is_file() else name


def run(*argv: str) -> None:
    subprocess.run(argv, cwd=ROOT, check=True)


def check_release_lock() -> None:
    expected = ROOT / "requirements-release.lock"
    with tempfile.TemporaryDirectory(prefix="hub-release-lock-") as directory:
        temporary = Path(directory)
        generated = temporary / "requirements-release.lock"
        subprocess.run(
            (
                tool("uv"),
                "export",
                "--frozen",
                "--no-dev",
                "--extra",
                "e2e",
                "--no-emit-project",
                "--no-annotate",
                "--no-header",
                "--cache-dir",
                str(temporary / "cache"),
                "--output-file",
                str(generated),
            ),
            cwd=ROOT,
            check=True,
            stdout=subprocess.DEVNULL,
        )
        if generated.read_bytes() != expected.read_bytes():
            raise RuntimeError(
                "requirements-release.lock is stale; regenerate it with the documented uv export"
            )


def main() -> int:
    run(sys.executable, "-m", "hermes_codex_router.privacy_scan", str(ROOT), "--history")
    check_release_lock()
    run(tool("ruff"), "format", "--check", ".")
    run(tool("ruff"), "check", ".")
    run(tool("pyright"))
    run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q")
    run(sys.executable, "-m", "hermes_codex_router.documentation_contract", str(ROOT))
    run(sys.executable, "-m", "hermes_codex_router.release_metadata", str(ROOT))
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
