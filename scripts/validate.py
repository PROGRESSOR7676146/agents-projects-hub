#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Sequence

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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repository validation; canonical by default")
    parser.add_argument("--profile", choices=("canonical", "focused"), default="canonical")
    parser.add_argument(
        "tests", nargs="*", help="focused only: dotted unittest modules/classes/methods"
    )
    args = parser.parse_args(argv)
    if args.tests and args.profile != "focused":
        parser.error("test selectors require --profile focused; canonical always runs all tests")
    if any(
        re.fullmatch(r"tests(?:\.[A-Za-z_][A-Za-z_0-9]*)+", name) is None for name in args.tests
    ):
        parser.error("test selectors must be dotted names under tests, not unittest options")

    stages: list[tuple[str, Callable[[], None]]] = [
        (
            "documentation",
            lambda: run(
                sys.executable, "-m", "hermes_codex_router.documentation_contract", str(ROOT)
            ),
        ),
        (
            "release metadata",
            lambda: run(sys.executable, "-m", "hermes_codex_router.release_metadata", str(ROOT)),
        ),
        (
            "example configuration",
            lambda: run(
                sys.executable,
                "-m",
                "hermes_codex_router.cli",
                "validate",
                "config/projects.example.json",
                "--allow-missing",
            ),
        ),
        ("release lock", check_release_lock),
        ("format", lambda: run(tool("ruff"), "format", "--check", ".")),
        ("lint", lambda: run(tool("ruff"), "check", ".")),
    ]
    if args.profile == "canonical":
        stages.extend(
            [
                (
                    "privacy/history",
                    lambda: run(
                        sys.executable,
                        "-m",
                        "hermes_codex_router.privacy_scan",
                        str(ROOT),
                        "--history",
                    ),
                ),
                ("types", lambda: run(tool("pyright"))),
                (
                    "full tests",
                    lambda: run(sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"),
                ),
            ]
        )
    else:
        stages.append(
            (
                "privacy/tree",
                lambda: run(sys.executable, "-m", "hermes_codex_router.privacy_scan", str(ROOT)),
            )
        )
        if args.tests:
            stages.append(
                ("selected tests", lambda: run(sys.executable, "-m", "unittest", *args.tests, "-q"))
            )

    label = (
        "Canonical validation"
        if args.profile == "canonical"
        else "Focused development checks (not canonical acceptance)"
    )
    print(label, flush=True)
    started = time.monotonic()
    for name, action in stages:
        stage_started = time.monotonic()
        print(f"RUN {name}", flush=True)
        try:
            action()
        except (subprocess.CalledProcessError, OSError, RuntimeError) as error:
            detail = (
                f"exit {error.returncode}"
                if isinstance(error, subprocess.CalledProcessError)
                else str(error)
            )
            print(
                f"FAIL {name} ({time.monotonic() - stage_started:.2f}s): {detail}", file=sys.stderr
            )
            return 1
        print(f"PASS {name} ({time.monotonic() - stage_started:.2f}s)", flush=True)
    print(f"{label} passed ({time.monotonic() - started:.2f}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
