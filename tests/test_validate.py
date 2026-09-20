from __future__ import annotations

import importlib.util
import io
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
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

        def run(*argv: str) -> None:
            command = " ".join(argv)
            calls.append(command)
            if failure and failure in command:
                raise subprocess.CalledProcessError(7, argv)

        output = io.StringIO()
        with (
            patch.object(validator, "run", side_effect=run),
            patch.object(validator, "check_release_lock", side_effect=lambda: calls.append("lock")),
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
        expensive = ("pyright", "unittest discover -s tests -q")
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

    def test_documentation_failure_prevents_typing_and_test_suite(self) -> None:
        code, calls, output = self.invoke([], "documentation_contract")
        self.assertEqual(code, 1)
        self.assertFalse(any("pyright" in call or "unittest" in call for call in calls))
        self.assertFalse(any("privacy_scan" in call for call in calls))
        self.assertIn("FAIL documentation", output)

    def test_focused_runs_only_requested_tests_and_labels_partial_evidence(self) -> None:
        code, calls, output = self.invoke(
            ["--profile", "focused", "tests.test_documentation_contract"]
        )
        self.assertEqual(code, 0)
        self.assertTrue(
            any("unittest tests.test_documentation_contract -q" in call for call in calls)
        )
        self.assertFalse(any("discover" in call or "pyright" in call for call in calls))
        privacy = next(call for call in calls if "privacy_scan" in call)
        self.assertNotIn("--history", privacy)
        self.assertIn("not canonical acceptance", output)

    def test_focused_without_selection_is_static_only(self) -> None:
        code, calls, _ = self.invoke(["--profile", "focused"])
        self.assertEqual(code, 0)
        self.assertFalse(any("unittest" in call or "pyright" in call for call in calls))

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


if __name__ == "__main__":
    unittest.main()
