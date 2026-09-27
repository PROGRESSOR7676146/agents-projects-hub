from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location(
    "repository_validate", Path(__file__).resolve().parents[1] / "scripts" / "validate.py"
)
assert _SPEC is not None and _SPEC.loader is not None
validator = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(validator)


class ValidationTests(unittest.TestCase):
    def invoke(self, args: list[str], failure: str = "") -> tuple[int, list[str], str]:
        calls: list[str] = []

        def run(*argv: str, **_kwargs: object) -> None:
            command = " ".join(argv)
            calls.append(command)
            if failure and failure in command:
                raise subprocess.CalledProcessError(7, argv)

        def run_test_modules(*, jobs: int, **_kwargs: object) -> None:
            calls.append(f"all test modules jobs={jobs}")
            if failure and failure in "all test modules":
                raise RuntimeError("1 of 2 test modules failed: test_example.py")

        output = io.StringIO()
        with (
            patch.object(validator, "run", side_effect=run),
            patch.object(validator, "check_release_lock", side_effect=lambda: calls.append("lock")),
            patch.object(validator, "run_test_modules", side_effect=run_test_modules),
            patch("sys.argv", ["validate.py", *args]),
            redirect_stdout(output),
            redirect_stderr(output),
        ):
            code = validator.main()
        return code, calls, output.getvalue()

    def test_canonical_keeps_all_guarantees_and_preflights_before_expensive_work(self) -> None:
        code, calls, _ = self.invoke([])
        self.assertEqual(code, 0)
        # Relative dependency groups, not an exact implementation-order snapshot.
        cheap = (
            "privacy_scan",
            "documentation_contract",
            "release_metadata",
            "cli validate config/projects.example.json --allow-missing",
            "lock",
            "format --check",
            "ruff check",
        )
        expensive = ("pyright", "all test modules")
        for marker in cheap + expensive:
            self.assertEqual(sum(marker in call for call in calls), 1, marker)
        first_expensive = min(
            index for index, call in enumerate(calls) if any(key in call for key in expensive)
        )
        for marker in cheap:
            self.assertLess(
                next(i for i, call in enumerate(calls) if marker in call), first_expensive
            )
        privacy = next(call for call in calls if "privacy_scan" in call)
        self.assertIn("--history", privacy)

    def test_canonical_test_parallelism_is_bounded_and_selectable(self) -> None:
        _, calls, _ = self.invoke([])
        default = next(call for call in calls if "all test modules" in call)
        self.assertIn(f"jobs={validator.default_test_jobs()}", default)
        self.assertGreaterEqual(validator.default_test_jobs(), 1)
        self.assertLessEqual(validator.default_test_jobs(), validator.MAX_TEST_JOBS)
        _, calls, _ = self.invoke(["--jobs", "1"])
        self.assertIn("all test modules jobs=1", calls)
        for jobs in ("0", str(validator.MAX_TEST_JOBS + 1), "many"):
            with self.subTest(jobs=jobs):
                with self.assertRaises(SystemExit) as error:
                    self.invoke(["--jobs", jobs])
                self.assertEqual(error.exception.code, 2)

    def test_test_module_failure_fails_the_canonical_gate(self) -> None:
        code, _, output = self.invoke([], "all test modules")
        self.assertEqual(code, 1)
        self.assertIn("FAIL full tests", output)
        self.assertIn("test_example.py", output)

    def test_commit_gate_runs_history_and_full_tests_but_leaves_types_to_push(self) -> None:
        code, calls, output = self.invoke(["--profile", "commit", "--jobs", "2"])
        self.assertEqual(code, 0)
        privacy = [call for call in calls if "privacy_scan" in call]
        self.assertEqual(len(privacy), 1)
        self.assertIn("--history", privacy[0])
        self.assertIn("all test modules jobs=2", calls)
        self.assertFalse(any("pyright" in call for call in calls))
        for marker in ("documentation_contract", "release_metadata", "lock", "ruff check"):
            self.assertLess(
                next(i for i, call in enumerate(calls) if marker in call),
                next(i for i, call in enumerate(calls) if "privacy_scan" in call),
            )
        self.assertIn("Commit gate (not canonical acceptance", output)
        with self.assertRaises(SystemExit) as error:
            self.invoke(["--profile", "commit", "tests.test_validate"])
        self.assertEqual(error.exception.code, 2)
        with self.assertRaises(SystemExit) as error:
            self.invoke(["--profile", "focused", "--jobs", "2"])
        self.assertEqual(error.exception.code, 2)

    def test_documentation_failure_prevents_typing_and_test_suite(self) -> None:
        code, calls, output = self.invoke([], "documentation_contract")
        self.assertEqual(code, 1)
        self.assertFalse(any("pyright" in call or "test modules" in call for call in calls))
        self.assertFalse(any("privacy_scan" in call for call in calls))
        self.assertIn("FAIL documentation", output)

    def test_focused_runs_only_requested_tests_and_labels_partial_evidence(self) -> None:
        code, calls, output = self.invoke(
            ["--profile", "focused", "tests.test_documentation_contract"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(any("unittest test_documentation_contract -q" in call for call in calls))
        self.assertFalse(
            any("discover" in call or "pyright" in call or "test modules" in call for call in calls)
        )
        privacy = next(call for call in calls if "privacy_scan" in call)
        self.assertNotIn("--history", privacy)
        self.assertIn("not canonical acceptance", output)

    def test_focused_selectors_import_sibling_fixtures_like_discovery(self) -> None:
        environments: list[dict[str, str]] = []

        def run(*argv: str, env: dict[str, str] | None = None) -> None:
            if "unittest" in argv:
                assert env is not None
                environments.append(env)

        with (
            patch.object(validator, "run", side_effect=run),
            patch.object(validator, "check_release_lock"),
            patch.dict(os.environ, {"PYTHONPATH": "/home/example/src"}),
            redirect_stdout(io.StringIO()),
        ):
            code = validator.main(["--profile", "focused", "tests.test_migrations.Class.test_x"])
        self.assertEqual(code, 0)
        self.assertEqual(len(environments), 1)
        self.assertEqual(
            environments[0]["PYTHONPATH"].split(os.pathsep),
            [str(validator.ROOT / "tests"), "/home/example/src"],
        )

    def test_focused_without_selection_is_static_only(self) -> None:
        code, calls, _ = self.invoke(["--profile", "focused"])
        self.assertEqual(code, 0)
        self.assertFalse(
            any("unittest" in call or "pyright" in call or "test modules" in call for call in calls)
        )

    def test_canonical_cannot_silently_become_partial(self) -> None:
        for args in (["tests.test_validate"], ["--profile", "focused", "--", "-h"]):
            with self.subTest(args=args):
                with self.assertRaises(SystemExit) as error:
                    self.invoke(args)
                self.assertEqual(error.exception.code, 2)

    def test_missing_tool_fails_with_stage_identity(self) -> None:
        with (
            patch.object(validator, "run", side_effect=FileNotFoundError("missing tool")),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as output,
        ):
            self.assertEqual(validator.main([]), 1)
        self.assertIn("FAIL", output.getvalue())


class TestModuleRunnerTests(unittest.TestCase):
    def fixture_root(self, modules: dict[str, str]) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "tests").mkdir()
        for name, source in modules.items():
            (root / "tests" / name).write_text(textwrap.dedent(source), encoding="utf-8")
        return root

    def test_each_module_runs_in_its_own_discovery_process_with_sibling_imports(self) -> None:
        root = self.fixture_root(
            {
                "shared_fixture.py": "VALUE = 7\n",
                "test_alpha.py": """
                    import unittest

                    import shared_fixture
                    import test_beta


                    class AlphaTests(unittest.TestCase):
                        def test_uses_sibling_modules(self):
                            self.assertEqual(shared_fixture.VALUE, 7)
                            self.assertTrue(test_beta.BetaTests)
                """,
                "test_beta.py": """
                    import unittest


                    class BetaTests(unittest.TestCase):
                        def test_one(self):
                            pass

                        def test_two(self):
                            pass
                """,
            }
        )
        output = io.StringIO()
        with redirect_stdout(output):
            validator.run_test_modules(jobs=2, root=root)
        self.assertIn("Ran 3 tests in 2 test modules", output.getvalue())

    def test_every_module_finishes_and_every_failure_is_named(self) -> None:
        root = self.fixture_root(
            {name: "" for name in ("test_alpha.py", "test_beta.py", "test_gamma.py")}
        )
        started: list[str] = []

        def fake_run(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.assertEqual(kwargs["cwd"], root)
            self.assertEqual(argv[1:5], ("-m", "unittest", "discover", "-s"))
            name = argv[argv.index("-p") + 1]
            started.append(name)
            if name == "test_alpha.py":
                return subprocess.CompletedProcess(
                    argv, 1, "Ran 2 tests in 0.010s\n\nFAILED (failures=1)\n"
                )
            if name == "test_beta.py":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output="partial output")
            return subprocess.CompletedProcess(argv, 0, "Ran 4 tests in 0.010s\n\nOK\n")

        errors = io.StringIO()
        with (
            patch.object(validator.subprocess, "run", side_effect=fake_run),
            redirect_stdout(io.StringIO()),
            redirect_stderr(errors),
            self.assertRaises(RuntimeError) as raised,
        ):
            validator.run_test_modules(jobs=3, root=root)
        self.assertEqual(sorted(started), ["test_alpha.py", "test_beta.py", "test_gamma.py"])
        self.assertIn("2 of 3 test modules failed", str(raised.exception))
        self.assertIn("test_alpha.py", str(raised.exception))
        self.assertIn("test_beta.py", str(raised.exception))
        self.assertNotIn("test_gamma.py", str(raised.exception))
        self.assertIn("FAILED (failures=1)", errors.getvalue())
        self.assertIn("timed out", errors.getvalue())

    def test_module_without_collected_tests_fails_closed(self) -> None:
        root = self.fixture_root({"test_empty.py": ""})

        def fake_run(argv: tuple[str, ...], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "Ran 0 tests in 0.000s\n\nOK\n")

        with (
            patch.object(validator.subprocess, "run", side_effect=fake_run),
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as errors,
            self.assertRaises(RuntimeError) as raised,
        ):
            validator.run_test_modules(jobs=1, root=root)
        self.assertIn("test_empty.py", str(raised.exception))
        self.assertIn("no tests collected", errors.getvalue())

    def test_missing_test_modules_fail_closed(self) -> None:
        root = self.fixture_root({"shared_fixture.py": ""})
        with self.assertRaises(RuntimeError) as raised:
            validator.run_test_modules(jobs=1, root=root)
        self.assertIn("no test modules", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
