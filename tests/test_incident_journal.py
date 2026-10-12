from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.incident_journal import (
    IncidentJournal,
    IncidentJournalBusy,
    IncidentJournalError,
)
from hermes_codex_router.operational_alert import OperationalAlert

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def alert(key: str = "example:private-key", code: str = "deployment_unhealthy"):
    return OperationalAlert(key, code, "error", "Local diagnostics require attention.")


class IncidentJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "incident-journal.json"

    def project(self, alerts=(), *, now=NOW):
        with IncidentJournal(self.path).cycle() as journal:
            return journal.project(tuple(alerts), now=now)

    def read(self):
        return json.loads(self.path.read_text())

    def test_continuity_resolution_rearm_and_private_payload(self) -> None:
        first = self.project((alert(),))
        identifier = first["episodes"][0]["id"]
        later = self.project((alert(),), now=NOW + timedelta(minutes=5))
        row = later["episodes"][0]
        self.assertEqual(row["id"], identifier)
        self.assertEqual(row["opened_at"], NOW.isoformat())
        self.assertEqual(row["last_seen_at"], (NOW + timedelta(minutes=5)).isoformat())
        resolved = self.project(now=NOW + timedelta(minutes=6))["episodes"][0]
        self.assertEqual(resolved["resolved_at"], (NOW + timedelta(minutes=6)).isoformat())
        rearmed = self.project((alert(),), now=NOW + timedelta(minutes=7))
        open_rows = [row for row in rearmed["episodes"] if row["resolved_at"] is None]
        self.assertEqual(len(open_rows), 1)
        self.assertNotEqual(open_rows[0]["id"], identifier)
        self.assertEqual(len(rearmed["episodes"]), 2)
        serialized = self.path.read_text()
        self.assertNotIn("example:private-key", serialized)
        self.assertEqual(row["message"], alert().message)
        for entry in self.path.parent.iterdir():
            self.assertEqual(entry.stat().st_mode & 0o777, 0o600)

    def test_empty_cycles_advance_time_and_code_changes_keep_active_identity(self) -> None:
        empty = self.project()
        self.assertEqual(empty["episodes"], [])
        initial = self.project((alert(),))
        changed = self.project(
            (alert(code="deployment_revision_mixed"),), now=NOW + timedelta(seconds=1)
        )
        self.assertEqual(initial["episodes"][0]["id"], changed["episodes"][0]["id"])
        self.assertEqual(changed["episodes"][0]["code"], "deployment_revision_mixed")
        self.assertNotEqual(empty["updated_at"], changed["updated_at"])

    def test_duplicate_keys_refuse_conflicts_without_replacing_snapshot(self) -> None:
        self.project((alert(), alert()))
        before = self.path.read_bytes()
        with self.assertRaises(IncidentJournalError):
            self.project(
                (alert(), alert(code="deployment_revision_unknown")), now=NOW + timedelta(seconds=1)
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_resolved_retention_boundary_and_old_open_episode(self) -> None:
        self.project((alert("example:closed"), alert("example:open")))
        self.project((alert("example:open"),), now=NOW + timedelta(seconds=1))
        retained = self.project((alert("example:open"),), now=NOW + timedelta(days=30, seconds=1))
        self.assertEqual(len(retained["episodes"]), 2)
        expired = self.project((alert("example:open"),), now=NOW + timedelta(days=30, seconds=2))
        self.assertEqual(len(expired["episodes"]), 1)
        self.assertIsNone(expired["episodes"][0]["resolved_at"])
        self.assertEqual(expired["episodes"][0]["opened_at"], NOW.isoformat())

    def test_storm_cap_is_visible_stable_and_never_fabricates_recovery(self) -> None:
        alerts = tuple(alert(f"example:{index}") for index in range(501))
        first = self.project(alerts)
        self.assertEqual(len(first["episodes"]), 500)
        self.assertEqual(first["omitted_open_count"], 1)
        self.assertTrue(all(row["resolved_at"] is None for row in first["episodes"]))
        second = self.project(tuple(reversed(alerts)), now=NOW + timedelta(seconds=1))
        self.assertEqual(
            {row["id"] for row in first["episodes"]}, {row["id"] for row in second["episodes"]}
        )

    def test_open_rows_take_priority_over_resolved_rows(self) -> None:
        self.project((alert("example:old"),))
        self.project(now=NOW + timedelta(seconds=1))
        result = self.project(
            tuple(alert(f"example:new:{i}") for i in range(500)), now=NOW + timedelta(seconds=2)
        )
        self.assertEqual(len(result["episodes"]), 500)
        self.assertEqual(result["omitted_resolved_count"], 1)
        self.assertTrue(all(row["resolved_at"] is None for row in result["episodes"]))

    def test_seed_is_private_separate_and_randomizes_opaque_prefix(self) -> None:
        first = self.project((alert(),))["episodes"][0]["id"]
        with tempfile.TemporaryDirectory() as directory:
            other = Path(directory) / self.path.name
            with IncidentJournal(other).cycle() as journal:
                second = journal.project((alert(),), now=NOW)["episodes"][0]["id"]
        self.assertNotEqual(first.split(".")[0], second.split(".")[0])
        seed = self.path.with_suffix(".seed").read_bytes()
        self.assertEqual(len(seed), 32)
        self.assertNotIn(seed.hex(), self.path.read_text())

    def test_changed_same_length_seed_refuses_without_invented_recovery(self) -> None:
        self.project((alert(),))
        before = self.path.read_bytes()
        seed_path = self.path.with_suffix(".seed")
        seed = seed_path.read_bytes()
        seed_path.write_bytes(bytes([seed[0] ^ 1]) + seed[1:])
        with self.assertRaises(IncidentJournalError):
            self.project((alert(),), now=NOW + timedelta(seconds=1))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertIsNone(self.read()["episodes"][0]["resolved_at"])
        seed_path.write_bytes(seed)
        recovered = self.project((alert(),), now=NOW + timedelta(seconds=2))
        self.assertEqual(recovered["episodes"][0]["id"], json.loads(before)["episodes"][0]["id"])

    def test_nonblocking_lock_does_not_replace_or_unlink_lock_inode(self) -> None:
        with IncidentJournal(self.path).cycle():
            inode = self.path.with_suffix(".lock").stat().st_ino
            with self.assertRaises(IncidentJournalBusy):
                with IncidentJournal(self.path).cycle():
                    self.fail("second cycle acquired the lock")
        self.project()
        self.assertEqual(self.path.with_suffix(".lock").stat().st_ino, inode)

    def test_publication_failure_preserves_previous_bytes_and_removes_temporary(self) -> None:
        self.project((alert(),))
        before = self.path.read_bytes()
        with patch("hermes_codex_router.incident_journal.os.replace", side_effect=OSError):
            with self.assertRaises(IncidentJournalError):
                self.project(now=NOW + timedelta(seconds=1))
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(any(entry.name.endswith(".tmp") for entry in self.path.parent.iterdir()))

    def test_corrupt_future_oversized_and_unsafe_files_preserved(self) -> None:
        for payload, mode in (
            (b"broken", 0o600),
            (b'{"schema_version":2}', 0o600),
            (b" " * (1024 * 1024 + 1), 0o600),
            (b"{}", 0o644),
        ):
            with self.subTest(size=len(payload), mode=mode):
                self.path.write_bytes(payload)
                self.path.chmod(mode)
                with self.assertRaises(IncidentJournalError):
                    self.project()
                self.assertEqual(self.path.read_bytes(), payload)
                self.assertEqual(self.path.stat().st_mode & 0o777, mode)
                self.path.unlink()

    def test_symlinks_and_special_files_refused_without_touching_target(self) -> None:
        target = self.path.parent / "example-target"
        target.write_text("unchanged")
        for suffix in (".json", ".seed", ".lock"):
            candidate = self.path.with_suffix(suffix)
            candidate.unlink(missing_ok=True)
            candidate.symlink_to(target)
            with self.subTest(suffix=suffix), self.assertRaises(IncidentJournalError):
                self.project()
            self.assertEqual(target.read_text(), "unchanged")
            candidate.unlink()
        os.mkfifo(self.path)
        with self.assertRaises(IncidentJournalError):
            self.project()

    def test_missing_seed_with_history_and_regressing_clock_refused(self) -> None:
        self.project((alert(),))
        before = self.path.read_bytes()
        with self.assertRaises(IncidentJournalError):
            self.project(now=NOW - timedelta(seconds=1))
        self.path.with_suffix(".seed").unlink()
        with self.assertRaises(IncidentJournalError):
            self.project()
        self.assertEqual(self.path.read_bytes(), before)

    def test_no_projection_outside_lock_or_with_naive_time(self) -> None:
        with self.assertRaises(IncidentJournalError):
            IncidentJournal(self.path).project((), now=NOW)
        with self.assertRaises(IncidentJournalError):
            self.project(now=datetime(2026, 1, 1))


if __name__ == "__main__":
    unittest.main()
