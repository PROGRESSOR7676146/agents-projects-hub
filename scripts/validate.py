#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
MAX_TEST_JOBS = 32
TEST_MODULE_TIMEOUT_SECONDS = 600
# The pattern whole-suite discovery passes to every module's ``load_tests``.
DISCOVERY_PATTERN = "test*.py"


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


def sibling_import_environment(root: Path = ROOT) -> dict[str, str]:
    """Let modules import sibling fixtures from ``tests`` exactly as discovery does."""
    environment = os.environ.copy()
    inherited = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(root / "tests"), inherited) if path
    )
    return environment


# Runs in a child process: run whole-suite discovery and record, for every
# module it loads tests from, the id of every test found in that module (a
# class imported into several modules counts in each, as in discovery). Also
# report every module under ``tests`` that defines ``load_tests``.
_DISCOVERY_PROBE = """
import json, os, sys, unittest
from unittest.loader import _FailedTest

found = {}

def tests_in(item):
    if isinstance(item, unittest.TestSuite):
        for child in item:
            yield from tests_in(child)
    else:
        yield item

class RecordingLoader(unittest.TestLoader):
    def loadTestsFromModule(self, module, *args, **kwargs):
        suite = super().loadTestsFromModule(module, *args, **kwargs)
        ids = [test.id() for test in tests_in(suite)]
        if ids:
            found.setdefault(module.__name__, []).extend(ids)
        return suite

suite = RecordingLoader().discover("tests", pattern=PATTERN, top_level_dir="tests")
for test in tests_in(suite):
    # Import failures and modules that skip themselves on import become
    # placeholder tests named after the module.
    placeholder = isinstance(test, _FailedTest) or (
        type(test).__name__ == "ModuleSkipped" and type(test).__module__ == "unittest.loader"
    )
    if placeholder and test.id() not in found.get(test._testMethodName, []):
        found.setdefault(test._testMethodName, []).append(test.id())
root = os.path.realpath("tests")

def under_tests(module):
    path = getattr(module, "__file__", None)
    if not isinstance(path, str):
        return False
    return os.path.commonpath([os.path.realpath(path), root]) == root

custom = sorted(
    name
    for name, module in list(sys.modules.items())
    if under_tests(module) and "load_tests" in getattr(module, "__dict__", {})
)
json.dump({"tests": found, "custom_loaders": custom}, sys.stdout)
""".replace("PATTERN", repr(DISCOVERY_PATTERN))

# Runs one module in a child process. The module is loaded with the pattern
# discovery uses, so its ``load_tests`` receives the same arguments, and the id
# of every test that starts is written to the report file named by argv[2].
_MODULE_RUNNER = """
import importlib, json, sys, unittest
from unittest.loader import _make_skipped_test
from unittest.suite import _ErrorHolder
from unittest.util import strclass

name, report = sys.argv[1], sys.argv[2]
started = []
setup_skips = []

def tests_in(item):
    if isinstance(item, unittest.TestSuite):
        for child in item:
            yield from tests_in(child)
    else:
        yield item

class Result(unittest.TextTestResult):
    def startTest(self, test):
        started.append(test.id())
        super().startTest(test)

    def addSkip(self, test, reason):
        # A module or class set-up that raises SkipTest skips its tests
        # without starting them, exactly as under discovery.
        if isinstance(test, _ErrorHolder):
            setup_skips.append(test.description)
        super().addSkip(test, reason)

loader = unittest.defaultTestLoader
try:
    module = importlib.import_module(name)
except unittest.SkipTest as skipped:
    # Discovery turns a module that skips itself on import into this test.
    suite = _make_skipped_test(name, skipped, loader.suiteClass)
else:
    suite = loader.loadTestsFromModule(module, pattern=PATTERN)
# The module and class unittest itself uses for set-up fixtures.
loaded = [
    [test.id(), type(test).__module__, strclass(type(test))] for test in tests_in(suite)
]
runner = unittest.TextTestRunner(
    resultclass=Result, verbosity=0, warnings=None if sys.warnoptions else "default"
)
result = runner.run(suite)
with open(report, "w", encoding="utf-8") as handle:
    json.dump({"started": started, "loaded": loaded, "setup_skips": setup_skips}, handle)
sys.exit(0 if result.wasSuccessful() else 1)
""".replace("PATTERN", repr(DISCOVERY_PATTERN))


def accounted_tests(report: dict[str, list[Any]]) -> list[str]:
    """Tests that started, plus those a module or class set-up skipped.

    A set-up skip names its exact module or class, as unittest's fixtures do,
    so a skip never covers a test of another module or class.
    """
    scopes: set[tuple[str, str]] = set()
    for description in report["setup_skips"]:
        kind, _, rest = str(description).partition(" (")
        if kind in {"setUpModule", "setUpClass"} and rest.endswith(")"):
            scopes.add((kind, rest[:-1]))
    skipped = [
        str(test_id)
        for test_id, module, test_class in report["loaded"]
        if ("setUpModule", str(module)) in scopes or ("setUpClass", str(test_class)) in scopes
    ]
    return [str(test) for test in report["started"]] + skipped


def _text(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def discoverable_test_files(root: Path = ROOT) -> list[str]:
    """Dotted names of every ``test*.py`` file discovery can import from ``tests``."""
    tests = root / "tests"
    names: list[str] = []
    for path in sorted(tests.rglob("test*.py")):
        parts = path.relative_to(tests).parts
        packages = [tests.joinpath(*parts[: index + 1]) for index in range(len(parts) - 1)]
        if all((package / "__init__.py").is_file() for package in packages):
            names.append(".".join((*parts[:-1], path.stem)))
    return names


def discover_test_modules(root: Path = ROOT) -> dict[str, list[str]]:
    """Return ``{dotted module: test ids}`` exactly as ``unittest`` discovery sees it.

    Tests are listed under the module discovery loaded them from, so an
    isolated run of that module imports the same modules and packages. Any
    module under ``tests`` that defines ``load_tests`` is refused: such a hook
    can build suites (other tests, parameterized instances, package-level
    wrappers) that an isolated run would not reproduce, and equal test ids
    cannot prove otherwise.
    """
    completed = subprocess.run(
        (sys.executable, "-c", _DISCOVERY_PROBE),
        cwd=root,
        env=sibling_import_environment(root),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TEST_MODULE_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("test discovery failed:\n" + completed.stderr.rstrip()[-4000:])
    report = json.loads(completed.stdout)
    if report["custom_loaders"]:
        raise RuntimeError(
            "load_tests hooks are not supported by the parallel runner; remove them from: "
            + ", ".join(str(name) for name in report["custom_loaders"])
        )
    return {str(name): [str(test) for test in tests] for name, tests in report["tests"].items()}


def composition_mismatch(discovered: Sequence[str], started: Sequence[str]) -> str | None:
    """Describe how the tests that ran differ from the tests discovery found."""
    missing = Counter(discovered) - Counter(started)
    extra = Counter(started) - Counter(discovered)
    if not missing and not extra:
        return None
    details = []
    if missing:
        details.append("not run: " + ", ".join(sorted(missing)[:5]))
    if extra:
        details.append("not discovered: " + ", ".join(sorted(extra)[:5]))
    return f"ran {len(started)} of {len(discovered)} discovered tests; " + "; ".join(details)


def run_test_modules(*, jobs: int, root: Path = ROOT) -> None:
    """Run every discovered test module in its own process, several at a time.

    Discovery supplies the dotted module names, including nested test
    packages, and the id of every test each module contributes. Every module
    then runs alone with the same import path and the discovery pattern, and
    the ids of the tests that actually started must equal the discovered ids;
    an equal count is not enough. A ``test*.py`` file that contributes no tests
    fails. All modules finish before failures are reported; none is skipped
    after a failure.
    """
    expected = discover_test_modules(root)
    # A package that skips itself on import hides its test files from discovery.
    skipped_packages = {
        name
        for name, tests in expected.items()
        if tests == [f"unittest.loader.ModuleSkipped.{name}"]
    }
    empty = sorted(
        name
        for name in set(discoverable_test_files(root)) - set(expected)
        if not any(name.startswith(package + ".") for package in skipped_packages)
    )
    modules = sorted(expected)
    if not modules and not empty:
        raise RuntimeError("no test modules discovered under tests/")
    environment = sibling_import_environment(root)

    with tempfile.TemporaryDirectory(prefix="hub-test-reports-") as reports:

        def run_module(name: str) -> tuple[str, str | None, int, str]:
            report = Path(reports) / f"{name}.json"
            argv = (sys.executable, "-c", _MODULE_RUNNER, name, str(report))
            try:
                completed = subprocess.run(
                    argv,
                    cwd=root,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=TEST_MODULE_TIMEOUT_SECONDS,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                reason = f"timed out after {TEST_MODULE_TIMEOUT_SECONDS}s"
                return name, reason, 0, _text(error.output)
            output = completed.stdout or ""
            try:
                run_report = json.loads(report.read_text(encoding="utf-8"))
                accounted = accounted_tests(run_report)
                started = len(run_report["started"])
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                reason = f"exit {completed.returncode}" if completed.returncode else "no run report"
                return name, reason, 0, output
            if completed.returncode != 0:
                return name, f"exit {completed.returncode}", started, output
            return name, composition_mismatch(expected[name], accounted), started, output

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(run_module, modules))
    failed = [(name, reason, output) for name, reason, _, output in results if reason]
    failed += [(name, "no tests collected", "") for name in empty]
    for name, reason, output in failed:
        print(f"--- {name}: {reason}", file=sys.stderr)
        if output:
            print(output.rstrip()[-8000:], file=sys.stderr)
    if failed:
        names = ", ".join(name for name, _, _ in failed)
        total_modules = len(modules) + len(empty)
        raise RuntimeError(f"{len(failed)} of {total_modules} test modules failed: {names}")
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
        (
            "hotspots",
            lambda: run(sys.executable, "-m", "hermes_codex_router.hotspot_audit", str(ROOT)),
        ),
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
