from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from hermes_codex_router.hotspot_audit import (
    BASELINE_PATH,
    PACKAGE_PATH,
    audit_hotspots,
    measure_hotspots,
)


def _entry(target: str, max_lines: int, **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "target": target,
        "max_lines": max_lines,
        "rationale": "Pre-existing Controller dispatcher; extraction is planned.",
        "owner": "repository owner",
        "next_review": "2026-12-31",
        "reopen_event": "Any growth or a new lifecycle branch.",
    }
    entry.update(overrides)
    return entry


class HotspotAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / PACKAGE_PATH).mkdir(parents=True)
        (self.root / BASELINE_PATH).parent.mkdir(parents=True)

    def write_module(self, name: str, function_lines: int, padding: int = 0) -> None:
        """A method of exactly ``function_lines`` lines, plus blank padding lines."""
        lines = ["class Service:", "    def handle(self):"]
        lines += [f"        value_{index} = {index}" for index in range(function_lines - 1)]
        lines += [""] * padding
        (self.root / PACKAGE_PATH / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def write_baseline(self, *entries: dict[str, object]) -> None:
        (self.root / BASELINE_PATH).write_text(
            json.dumps({"schema_version": 1, "hotspots": list(entries)}), encoding="utf-8"
        )

    def test_measures_files_and_qualified_functions_above_thresholds(self) -> None:
        self.write_module("service.py", 220, padding=1400)
        self.write_module("small.py", 20)
        measured = measure_hotspots(self.root)
        self.assertEqual(
            measured,
            {
                "src/hermes_codex_router/service.py": 1621,
                "src/hermes_codex_router/service.py::Service.handle": 220,
            },
        )

    def test_recorded_hotspot_within_its_bound_passes(self) -> None:
        self.write_module("service.py", 220)
        self.write_baseline(_entry("src/hermes_codex_router/service.py::Service.handle", 220))
        result = audit_hotspots(self.root, today=date(2026, 10, 1))
        self.assertEqual((result.errors, result.debts), ((), ()))

    def test_growth_and_unrecorded_hotspots_fail(self) -> None:
        self.write_module("service.py", 230)
        self.write_module("other.py", 205)
        self.write_baseline(_entry("src/hermes_codex_router/service.py::Service.handle", 220))
        result = audit_hotspots(self.root, today=date(2026, 10, 1))
        self.assertEqual(
            result.errors,
            (
                "new hotspot needs a bounded exception: "
                "src/hermes_codex_router/other.py::Service.handle (205 lines)",
                "hotspot grew without a bounded exception: "
                "src/hermes_codex_router/service.py::Service.handle 230 > 220 lines",
            ),
        )

    def test_shrinkage_stale_entries_and_overdue_reviews_are_debt(self) -> None:
        self.write_module("service.py", 210)
        self.write_baseline(
            _entry("src/hermes_codex_router/service.py::Service.handle", 220),
            _entry("src/hermes_codex_router/gone.py", 1600, next_review="2026-09-01"),
        )
        result = audit_hotspots(self.root, today=date(2026, 10, 1))
        self.assertEqual(result.errors, ())
        self.assertEqual(
            result.debts,
            (
                "lower recorded bound: src/hermes_codex_router/service.py::Service.handle "
                "is 210 of 220 lines",
                "remove resolved hotspot entry: src/hermes_codex_router/gone.py",
                "bounded exception review overdue: src/hermes_codex_router/gone.py (2026-09-01)",
            ),
        )

    def test_bounded_exception_requires_every_rule_11_field(self) -> None:
        self.write_module("service.py", 220)
        self.write_baseline(
            _entry(
                "src/hermes_codex_router/service.py::Service.handle",
                220,
                owner="",
                next_review="soon",
            )
        )
        result = audit_hotspots(self.root, today=date(2026, 10, 1))
        self.assertEqual(
            result.errors,
            (
                "hotspot entry src/hermes_codex_router/service.py::Service.handle "
                "needs a non-empty owner",
                "hotspot entry src/hermes_codex_router/service.py::Service.handle "
                "needs next_review as YYYY-MM-DD",
            ),
        )

    def test_repository_baseline_matches_the_current_tree(self) -> None:
        root = Path(__file__).resolve().parents[1]
        result = audit_hotspots(root)
        self.assertEqual(result.errors, ())


if __name__ == "__main__":
    unittest.main()
