from __future__ import annotations

import importlib.util
import io
import json
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
            # A distinctive marker: a checkout path may itself contain "lock".
            patch.object(
                validator,
                "check_release_lock",
                side_effect=lambda: calls.append("release-lock check"),
            ),
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
            "release-lock check",
            "hotspot_audit",
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
        for marker in (
            "documentation_contract",
            "release_metadata",
            "release-lock check",
            "ruff check",
        ):
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
            path = root / "tests" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(source), encoding="utf-8")
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

    def test_nested_packages_and_repeated_basenames_each_run_once(self) -> None:
        passing = """
            import unittest


            class Tests(unittest.TestCase):
                def test_passes(self):
                    pass
        """
        failing = """
            import unittest


            class Tests(unittest.TestCase):
                def test_regression(self):
                    self.fail("nested regression")
        """
        root = self.fixture_root(
            {
                "test_top.py": passing,
                "nested/__init__.py": "",
                "nested/test_top.py": failing,
                "loose/test_ignored.py": failing,
            }
        )
        errors = io.StringIO()
        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(errors),
            self.assertRaises(RuntimeError) as raised,
        ):
            validator.run_test_modules(jobs=2, root=root)
        self.assertIn("1 of 2 test modules failed: nested.test_top", str(raised.exception))
        self.assertIn("nested regression", errors.getvalue())
        (root / "tests" / "nested" / "test_top.py").write_text(
            textwrap.dedent(passing), encoding="utf-8"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            validator.run_test_modules(jobs=2, root=root)
        self.assertIn("Ran 2 tests in 2 test modules", output.getvalue())

    def test_every_module_finishes_and_every_failure_is_named(self) -> None:
        root = self.fixture_root({})
        expected = {
            name: [f"{name}.Tests.test_{index}" for index in range(count)]
            for name, count in (("test_alpha", 2), ("test_beta", 1), ("test_gamma", 4))
        }
        expected["test_delta"] = [f"test_delta.Tests.test_{index}" for index in range(3)]
        started: list[str] = []

        def fake_run(argv: tuple[str, ...], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.assertEqual(kwargs["cwd"], root)
            self.assertEqual(argv[1], "-c")
            name, report = argv[3], Path(argv[4])
            started.append(name)
            if name == "test_beta":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"partial \xff")
            ran = expected[name][:2] if name == "test_delta" else expected[name]
            loaded = [[test, name, f"{name}.Tests"] for test in ran]
            report.write_text(
                json.dumps({"started": ran, "loaded": loaded, "setup_skips": []}),
                encoding="utf-8",
            )
            if name == "test_alpha":
                return subprocess.CompletedProcess(
                    argv, 1, "Ran 2 tests in 0.010s\n\nFAILED (failures=1)\n"
                )
            return subprocess.CompletedProcess(argv, 0, "OK\n")

        errors = io.StringIO()
        with (
            patch.object(validator, "discover_test_modules", return_value=expected),
            patch.object(validator, "discoverable_test_files", return_value=sorted(expected)),
            patch.object(validator.subprocess, "run", side_effect=fake_run),
            redirect_stdout(io.StringIO()),
            redirect_stderr(errors),
            self.assertRaises(RuntimeError) as raised,
        ):
            validator.run_test_modules(jobs=3, root=root)
        self.assertEqual(sorted(started), sorted(expected))
        message = str(raised.exception)
        self.assertIn("3 of 4 test modules failed", message)
        for name in ("test_alpha", "test_beta", "test_delta"):
            self.assertIn(name, message)
        self.assertNotIn("test_gamma", message)
        self.assertIn("FAILED (failures=1)", errors.getvalue())
        self.assertIn("timed out", errors.getvalue())
        self.assertIn("partial \ufffd", errors.getvalue())
        self.assertIn(
            "ran 2 of 3 discovered tests; not run: test_delta.Tests.test_2", errors.getvalue()
        )

    def test_custom_load_tests_hooks_are_refused(self) -> None:
        # A hook can build a suite that an isolated module run would not
        # reproduce, even with the same test ids.
        cases = {
            "module": {
                "test_selective.py": """
                    import unittest


                    class Smoke(unittest.TestCase):
                        def test_smoke(self):
                            pass


                    class Regression(unittest.TestCase):
                        def test_regression(self):
                            self.fail("discovered regression")


                    def load_tests(loader, tests, pattern):
                        return loader.loadTestsFromTestCase(Regression if pattern else Smoke)
                """,
            },
            "package": {
                "nested/__init__.py": """
                    import unittest

                    from nested.test_parameterized import Check


                    def load_tests(loader, tests, pattern):
                        return unittest.TestSuite([Check(strict=True)])
                """,
                "nested/test_parameterized.py": """
                    import unittest


                    class Check(unittest.TestCase):
                        def __init__(self, methodName="runTest", strict=False):
                            super().__init__(methodName)
                            self.strict = strict

                        def runTest(self):
                            self.assertFalse(self.strict, "strict regression")
                """,
            },
        }
        for kind, modules in cases.items():
            with self.subTest(kind=kind):
                root = self.fixture_root(modules)
                with (
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                    self.assertRaises(RuntimeError) as raised,
                ):
                    validator.run_test_modules(jobs=1, root=root)
                message = str(raised.exception)
                self.assertIn("load_tests hooks are not supported", message)
                self.assertIn("test_selective" if kind == "module" else "nested", message)

    def test_load_tests_in_any_module_under_tests_is_refused(self) -> None:
        root = self.fixture_root(
            {
                "helper.py": """
                    import unittest


                    class Check(unittest.TestCase):
                        strict = True

                        def test_check(self):
                            self.assertFalse(self.strict, "strict regression")


                    def load_tests(loader, tests, pattern):
                        for test in tests:
                            for case in test:
                                case.strict = False
                        return tests
                """,
                "nested/__init__.py": "from helper import Check\n",
                "test_entry.py": """
                    import unittest


                    class Entry(unittest.TestCase):
                        def test_entry(self):
                            pass
                """,
            }
        )
        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()),
            self.assertRaises(RuntimeError) as raised,
        ):
            validator.run_test_modules(jobs=2, root=root)
        self.assertIn("load_tests hooks are not supported", str(raised.exception))
        self.assertIn("helper", str(raised.exception))

    def test_tests_run_in_the_module_discovery_found_them_in(self) -> None:
        # The package changes an imported class, so the test fails under
        # discovery; running the class's own module alone would pass it.
        root = self.fixture_root(
            {
                "helper.py": """
                    import unittest


                    class Check(unittest.TestCase):
                        strict = False

                        def test_check(self):
                            self.assertFalse(self.strict, "strict regression")
                """,
                "nested/__init__.py": "from helper import Check\n\nCheck.strict = True\n",
                "test_entry.py": """
                    import unittest


                    class Entry(unittest.TestCase):
                        def test_entry(self):
                            pass
                """,
            }
        )
        errors = io.StringIO()
        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(errors),
            self.assertRaises(RuntimeError) as raised,
        ):
            validator.run_test_modules(jobs=2, root=root)
        self.assertIn("1 of 2 test modules failed: nested", str(raised.exception))
        self.assertIn("strict regression", errors.getvalue())

    def test_set_up_and_import_skips_count_as_discovery_counts_them(self) -> None:
        cases = {
            "module set-up": """
                import unittest


                def setUpModule():
                    raise unittest.SkipTest("example dependency unavailable")


                class Tests(unittest.TestCase):
                    def test_one(self):
                        pass
            """,
            "class set-up": """
                import unittest


                class Skipped(unittest.TestCase):
                    @classmethod
                    def setUpClass(cls):
                        raise unittest.SkipTest("example dependency unavailable")

                    def test_one(self):
                        pass


                class Running(unittest.TestCase):
                    def test_two(self):
                        pass
            """,
            "import": """
                import unittest

                raise unittest.SkipTest("example dependency unavailable")
            """,
        }
        for kind, source in cases.items():
            with self.subTest(kind=kind):
                root = self.fixture_root({"test_example.py": source})
                output = io.StringIO()
                with redirect_stdout(output), redirect_stderr(io.StringIO()):
                    validator.run_test_modules(jobs=1, root=root)
                self.assertIn("in 1 test modules", output.getvalue())

    def test_a_module_skip_never_covers_a_class_from_another_module(self) -> None:
        # The package skips its own set-up; the class it imports from a
        # submodule still runs, and its test must be accounted for by running.
        root = self.fixture_root(
            {
                "example/__init__.py": """
                    import unittest

                    from example.helper import B


                    def setUpModule():
                        raise unittest.SkipTest("example dependency unavailable")


                    class A(unittest.TestCase):
                        def test_a(self):
                            pass
                """,
                "example/helper.py": """
                    import unittest


                    class B(unittest.TestCase):
                        def test_b(self):
                            pass
                """,
                "example/test_entry.py": """
                    import unittest


                    class Entry(unittest.TestCase):
                        def test_entry(self):
                            pass
                """,
            }
        )
        report = {
            "started": ["example.helper.B.test_b"],
            "loaded": [
                ["example.A.test_a", "example", "example.A"],
                ["example.helper.B.test_b", "example.helper", "example.helper.B"],
            ],
            "setup_skips": ["setUpModule (example)"],
        }
        self.assertEqual(
            sorted(validator.accounted_tests(report)),
            ["example.A.test_a", "example.helper.B.test_b"],
        )
        # A skipped module never accounts for a test of another module that did not run.
        report["started"] = []
        self.assertEqual(validator.accounted_tests(report), ["example.A.test_a"])
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            validator.run_test_modules(jobs=1, root=root)
        self.assertIn("in 2 test modules", output.getvalue())

    def test_test_files_of_a_package_that_skips_on_import_are_not_empty(self) -> None:
        root = self.fixture_root(
            {
                "example/__init__.py": """
                    import unittest

                    raise unittest.SkipTest("example dependency unavailable")
                """,
                "example/test_child.py": """
                    import unittest


                    class Child(unittest.TestCase):
                        def test_child(self):
                            pass
                """,
            }
        )
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            validator.run_test_modules(jobs=1, root=root)
        self.assertIn("in 1 test modules", output.getvalue())

    def test_a_skipped_class_does_not_hide_a_failure_beside_it(self) -> None:
        root = self.fixture_root(
            {
                "test_example.py": """
                    import unittest


                    class Skipped(unittest.TestCase):
                        @classmethod
                        def setUpClass(cls):
                            raise unittest.SkipTest("example dependency unavailable")

                        def test_one(self):
                            pass


                    class Failing(unittest.TestCase):
                        def test_two(self):
                            self.fail("example regression")
                """,
            }
        )
        errors = io.StringIO()
        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(errors),
            self.assertRaises(RuntimeError),
        ):
            validator.run_test_modules(jobs=1, root=root)
        self.assertIn("example regression", errors.getvalue())

    def test_composition_mismatch_names_missing_and_extra_tests(self) -> None:
        self.assertIsNone(
            validator.composition_mismatch(["m.A.test", "m.B.test"], ["m.B.test", "m.A.test"])
        )
        message = validator.composition_mismatch(["m.Discovered.test"], ["m.Substitute.test"])
        assert message is not None
        self.assertIn("ran 1 of 1 discovered tests", message)
        self.assertIn("not run: m.Discovered.test", message)
        self.assertIn("not discovered: m.Substitute.test", message)
        duplicated = validator.composition_mismatch(["m.A.test", "m.A.test"], ["m.A.test"])
        self.assertIn("not run: m.A.test", duplicated or "")

    def test_real_timeout_keeps_the_output_printed_before_it(self) -> None:
        root = self.fixture_root(
            {
                "test_slow.py": """
                    import sys
                    import time
                    import unittest


                    class SlowTests(unittest.TestCase):
                        def test_hangs(self):
                            print("MARKER-BEFORE-TIMEOUT", flush=True)
                            time.sleep(30)
                """,
            }
        )
        errors = io.StringIO()
        with (
            patch.object(validator, "TEST_MODULE_TIMEOUT_SECONDS", 3),
            redirect_stdout(io.StringIO()),
            redirect_stderr(errors),
            self.assertRaises(RuntimeError) as raised,
        ):
            validator.run_test_modules(jobs=1, root=root)
        self.assertIn("test_slow", str(raised.exception))
        self.assertIn("timed out after 3s", errors.getvalue())
        self.assertIn("MARKER-BEFORE-TIMEOUT", errors.getvalue())

    def test_module_without_collected_tests_fails_closed(self) -> None:
        root = self.fixture_root(
            {
                "test_empty.py": "VALUE = 1\n",
                "test_ok.py": """
                    import unittest


                    class Tests(unittest.TestCase):
                        def test_ok(self):
                            pass
                """,
            }
        )
        with (
            redirect_stdout(io.StringIO()),
            redirect_stderr(io.StringIO()) as errors,
            self.assertRaises(RuntimeError) as raised,
        ):
            validator.run_test_modules(jobs=1, root=root)
        self.assertIn("1 of 2 test modules failed: test_empty", str(raised.exception))
        self.assertIn("no tests collected", errors.getvalue())

    def test_missing_test_modules_fail_closed(self) -> None:
        root = self.fixture_root({"shared_fixture.py": ""})
        with self.assertRaises(RuntimeError) as raised:
            validator.run_test_modules(jobs=1, root=root)
        self.assertIn("no test modules", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
