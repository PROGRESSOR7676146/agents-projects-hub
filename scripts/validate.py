#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
MAX_TEST_JOBS = 32
TEST_MODULE_TIMEOUT_SECONDS = 600
_RAN_TESTS = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)


def tool(name: str) -> str:
    sibling = Path(sys.executable).with_name(name)
    return str(sibling) if sibling.is_file() else name


def run(*argv: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(argv, cwd=ROOT, check=True, env=env)


def default_test_jobs() -> int:
    return max(1, min(8, os.cpu_count() or 1))


def parse_test_jobs(value: str) -> int:
    try:
        jobs = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be an integer") from None
    if not 1 <= jobs <= MAX_TEST_JOBS:
        raise argparse.ArgumentTypeError(f"must be between 1 and {MAX_TEST_JOBS}")
    return jobs


def sibling_import_environment() -> dict[str, str]:
    """Let selected modules import sibling fixtures exactly as discovery does."""
    environment = os.environ.copy()
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(ROOT / "tests"), inherited) if path
    )
    return environment


def run_test_modules(*, jobs: int, root: Path = ROOT) -> None:
    """Run every test module in its own discovery process, several at a time.

    Each process uses ``unittest discover`` rooted at ``tests`` with a single
    file pattern, so imports behave as in whole-suite discovery. All modules
    finish before failures are reported; none is skipped after a failure.
    """
    modules = sorted(path.name for path in (root / "tests").glob("test*.py"))
    if not modules:
        raise RuntimeError("no test modules discovered under tests/")

    def run_module(name: str) -> tuple[str, str | None, int, str]:
        argv = (sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", name, "-q")
        try:
            completed = subprocess.run(
                argv,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=TEST_MODULE_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            partial = error.output if isinstance(error.output, str) else ""
            return name, f"timed out after {TEST_MODULE_TIMEOUT_SECONDS}s", 0, partial
        output = completed.stdout or ""
        counted = _RAN_TESTS.search(output)
        count = int(counted.group(1)) if counted else 0
        if completed.returncode != 0:
            return name, f"exit {completed.returncode}", count, output
        if count == 0:
            return name, "no tests collected", 0, output
        return name, None, count, output

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(run_module, modules))
    failed = [(name, reason, output) for name, reason, _, output in results if reason]
    for name, reason, output in failed:
        print(f"--- {name}: {reason}", file=sys.stderr)
        print(output.rstrip()[-8000:], file=sys.stderr)
    if failed:
        names = ", ".join(name for name, _, _ in failed)
        raise RuntimeError(f"{len(failed)} of {len(modules)} test modules failed: {names}")
    total = sum(count for _, _, count, _ in results)
    print(f"Ran {total} tests in {len(modules)} test modules with {jobs} parallel jobs")


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
    parser.add_argument(
        "--profile", choices=("canonical", "commit", "focused"), default="canonical"
    )
    parser.add_argument(
        "--jobs",
        type=parse_test_jobs,
        help=f"canonical/commit: test modules run in parallel (1-{MAX_TEST_JOBS})",
    )
    parser.add_argument(
        "tests", nargs="*", help="focused only: dotted unittest modules/classes/methods"
    )
    args = parser.parse_args(argv)
    if args.tests and args.profile != "focused":
        parser.error("test selectors require --profile focused; other profiles run all tests")
    if args.jobs is not None and args.profile == "focused":
        parser.error("--jobs applies only to profiles that run the full test suite")
    jobs = default_test_jobs() if args.jobs is None else args.jobs
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
    if args.profile in {"canonical", "commit"}:
        stages.append(
            (
                "privacy/history",
                lambda: run(
                    sys.executable,
                    "-m",
                    "hermes_codex_router.privacy_scan",
                    str(ROOT),
                    "--history",
                ),
            )
        )
        # The commit gate leaves whole-project typing to the pre-push
        # canonical run and CI; every other canonical guarantee applies.
        if args.profile == "canonical":
            stages.append(("types", lambda: run(tool("pyright"))))
        stages.append(("full tests", lambda: run_test_modules(jobs=jobs)))
    else:
        stages.append(
            (
                "privacy/tree",
                lambda: run(sys.executable, "-m", "hermes_codex_router.privacy_scan", str(ROOT)),
            )
        )
        if args.tests:
            # Discovery imports modules from inside tests/, where they import
            # sibling fixtures as top-level modules; select them the same way.
            selected = [name.removeprefix("tests.") for name in args.tests]
            stages.append(
                (
                    "selected tests",
                    lambda: run(
                        sys.executable,
                        "-m",
                        "unittest",
                        *selected,
                        "-q",
                        env=sibling_import_environment(),
                    ),
                )
            )

    label = {
        "canonical": "Canonical validation",
        "commit": "Commit gate (not canonical acceptance; types run at push)",
        "focused": "Focused development checks (not canonical acceptance)",
    }[args.profile]
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
