"""Causal producer continuity and defensive SQL, using fictional local polls."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_codex_router.state import HubState
from hermes_codex_router.state_errors import StateError
from tests.schema_fixtures import project_historical_database


class TelegramIngressContinuityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "example-polls.db"
        self.state = HubState.open(self.path, codex_permission_profile=None)
        self.addCleanup(lambda: self.state.close())
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.owner = self.state.telegram_ingress.register(
            "hub", instance_token="example-original-publisher", previous_epoch=0, now=self.now
        )

    def poll(self, sequence: int, success: bool = False) -> bool:
        return self.state.telegram_ingress.record_poll(
            self.owner,
            sequence=sequence,
            succeeded=success,
            observed_at=self.now + timedelta(seconds=sequence),
        )

    def watermark(self) -> dict | None:
        row = self.state._connection.execute(
            "SELECT * FROM telegram_ingress_watermarks WHERE identity='hub'"
        ).fetchone()
        return None if row is None else dict(row)

    def snapshot(self) -> tuple:
        return tuple(
            (
                table,
                tuple(
                    tuple(row) for row in self.state._connection.execute(f"SELECT * FROM {table}")
                ),
            )
            for table in ("telegram_group_ingress", "telegram_ingress_watermarks")
        )

    def project50(self) -> None:
        old = self.path.with_name("example-schema50.db")
        project_historical_database(self.path, old, 50)
        self.state.close()
        self.state = HubState.open(old, codex_permission_profile=None)
        self.assertIsNone(self.watermark())

    def test_gaps_before_threshold_do_not_invent_established_failure(self) -> None:
        self.poll(1)
        self.poll(2)
        self.poll(4)
        mark = self.watermark()
        assert mark is not None
        self.assertIsNone(mark["failure_witness_epoch"])
        self.poll(5)
        self.poll(6)
        mark = self.watermark()
        assert mark is not None
        self.assertEqual((mark["failure_witness_epoch"], mark["failure_witness_sequence"]), (1, 6))
        self.assertEqual(
            mark["failure_threshold_at"], (self.now + timedelta(seconds=6)).isoformat()
        )
        self.poll(8)
        self.assertEqual(self.watermark(), mark)

    def test_first_new_failed_gap_adopts_projected50_threshold_with_actual_witness(self) -> None:
        for sequence in (1, 2, 3):
            self.poll(sequence)
        self.project50()
        self.poll(5)
        mark = self.watermark()
        assert mark is not None
        self.assertEqual((mark["failure_witness_epoch"], mark["failure_witness_sequence"]), (1, 5))
        self.assertEqual(
            mark["failure_threshold_at"], (self.now + timedelta(seconds=3)).isoformat()
        )
        ledger = self.state.telegram_ingress.read("hub")
        assert ledger is not None
        self.assertEqual(ledger.evidence.failure_streak, 1)
        self.assertIsNone(ledger.evidence.failure_threshold_at)
        self.state.telegram_ingress.register(
            "hub",
            instance_token="example-replacement-publisher",
            previous_epoch=1,
            now=self.now + timedelta(seconds=6),
        )
        self.assertEqual(self.watermark(), mark)

    def test_historical_registration_duplicate_and_refused_samples_cannot_adopt(self) -> None:
        for sequence in (1, 2, 3):
            self.poll(sequence)
        self.project50()
        self.assertTrue(self.poll(3))
        self.assertIsNone(self.watermark())
        with self.assertRaises(StateError):
            self.poll(2)
        self.assertIsNone(self.watermark())
        self.state.telegram_ingress.register(
            "hub",
            instance_token=self.owner.instance_token,
            previous_epoch=0,
            now=self.now + timedelta(seconds=4),
        )
        self.assertIsNone(self.watermark())
        self.state.telegram_ingress.register(
            "hub",
            instance_token="example-new-publisher",
            previous_epoch=1,
            now=self.now + timedelta(seconds=4),
        )
        self.assertFalse(self.poll(5))
        self.assertIsNone(self.watermark())

    def test_success_gap_recovers_and_weak_historical_streaks_do_not_adopt(self) -> None:
        for streak in (1, 2, 3):
            with self.subTest(streak=streak), tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / "example-current.db"
                with closing(HubState.open(source, codex_permission_profile=None)) as state:
                    owner = state.telegram_ingress.register(
                        "hub",
                        instance_token="example-historical-publisher",
                        previous_epoch=0,
                        now=self.now,
                    )
                    for sequence in range(1, streak + 1):
                        state.telegram_ingress.record_poll(
                            owner, sequence=sequence, succeeded=False, observed_at=self.now
                        )
                target = source.with_name("example-old.db")
                project_historical_database(source, target, 50)
                with closing(HubState.open(target, codex_permission_profile=None)) as state:
                    state.telegram_ingress.record_poll(
                        owner, sequence=5, succeeded=streak == 3, observed_at=self.now
                    )
                    row = state._connection.execute(
                        "SELECT * FROM telegram_ingress_watermarks"
                    ).fetchone()
                    assert row is not None
                    self.assertIsNone(row["failure_witness_epoch"])
                    self.assertEqual(row["success_sequence"], 5 if streak == 3 else None)

    def test_watermark_write_and_commit_faults_roll_back_both_ledgers(self) -> None:
        for failed_action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_TRANSACTION):
            before = self.snapshot()

            def deny(action, first, second, database, trigger):
                blocked = action == failed_action and (
                    first == "telegram_ingress_watermarks"
                    if action == sqlite3.SQLITE_INSERT
                    else first == "COMMIT"
                )
                return sqlite3.SQLITE_DENY if blocked else sqlite3.SQLITE_OK

            self.state._connection.set_authorizer(deny)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    self.poll(1)
            finally:
                self.state._connection.set_authorizer(None)
            self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.poll(1))
        self.assertIsNotNone(self.watermark())

    def test_competing_epoch_cannot_change_retained_watermark(self) -> None:
        for sequence in (1, 2, 3):
            self.poll(sequence)
        before = self.watermark()
        with closing(HubState.open(self.path, codex_permission_profile=None)) as other:
            newer = other.telegram_ingress.register(
                "hub",
                instance_token="example-competing-publisher",
                previous_epoch=1,
                now=self.now + timedelta(seconds=4),
            )
            self.assertFalse(self.poll(5, True))
            self.assertEqual(self.watermark(), before)
            self.assertTrue(
                other.telegram_ingress.record_poll(
                    newer, sequence=1, succeeded=True, observed_at=self.now + timedelta(seconds=5)
                )
            )
        mark = self.watermark()
        assert mark is not None
        self.assertEqual((mark["success_epoch"], mark["success_sequence"]), (2, 1))
        self.assertIsNone(mark["failure_threshold_at"])

    def test_replace_identity_and_rowid_cannot_erase_ingress_or_watermark(self) -> None:
        self.poll(1, True)
        self.state.telegram_ingress.register(
            "codex",
            instance_token="example-codex-publisher",
            previous_epoch=0,
            now=self.now + timedelta(seconds=2),
        )
        self.state._connection.execute("PRAGMA recursive_triggers=OFF")
        for table in ("telegram_group_ingress", "telegram_ingress_watermarks"):
            before = self.snapshot()
            row = self.state._connection.execute(
                f"SELECT rowid,* FROM {table} WHERE identity='hub'"
            ).fetchone()
            assert row is not None
            names = list(row.keys())
            quoted = ",".join(names)
            placeholders = ",".join("?" for _ in names)
            with self.assertRaises(sqlite3.IntegrityError), self.state._immediate_transaction():
                self.state._connection.execute(
                    f"INSERT OR REPLACE INTO {table} ({quoted}) VALUES ({placeholders})", tuple(row)
                )
            with self.assertRaises(sqlite3.IntegrityError), self.state._immediate_transaction():
                self.state._connection.execute(
                    f"UPDATE OR REPLACE {table} SET rowid=100 WHERE identity='hub'"
                )
            self.assertEqual(self.snapshot(), before)
        victim = self.state._connection.execute(
            "SELECT rowid FROM telegram_group_ingress WHERE identity='codex'"
        ).fetchone()
        assert victim is not None
        with self.assertRaises(sqlite3.IntegrityError), self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE OR REPLACE telegram_group_ingress SET rowid=?,poll_sequence=2 WHERE identity='hub'",
                (victim[0],),
            )
        self.assertEqual(self.snapshot(), before)

    def test_null_partial_and_noninteger_cursor_bundles_are_refused(self) -> None:
        for sql in (
            "INSERT INTO telegram_ingress_watermarks(identity) VALUES(NULL)",
            "INSERT INTO telegram_ingress_watermarks(identity,success_sequence,success_at) VALUES('hub',1,'example')",
            "INSERT INTO telegram_ingress_watermarks(identity,failure_witness_epoch,failure_threshold_at) VALUES('hub',1,'example')",
            "INSERT INTO telegram_ingress_watermarks(identity,success_epoch,success_sequence,success_at) VALUES('hub',1.5,1,'example')",
        ):
            with (
                self.subTest(sql=sql),
                self.assertRaises(sqlite3.IntegrityError),
                self.state._immediate_transaction(),
            ):
                self.state._connection.execute(sql)
            self.assertIsNone(self.watermark())

    def test_fractional_legacy_streak_or_sequence_cannot_be_normalized_or_adopted(self) -> None:
        for sequence in (1, 2, 3):
            self.poll(sequence)
        self.project50()
        trigger = self.state._connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='telegram_group_ingress_fence'"
        ).fetchone()
        assert trigger is not None
        for column, value in (("failure_streak", 3.5), ("poll_sequence", 3.5), ("epoch", 1.5)):
            with self.subTest(column=column):
                with self.state._immediate_transaction():
                    self.state._connection.execute("DROP TRIGGER telegram_group_ingress_fence")
                    self.state._connection.execute(
                        f"UPDATE telegram_group_ingress SET {column}=?", (value,)
                    )
                before = self.snapshot()
                if column == "epoch":
                    self.assertFalse(self.poll(5))
                else:
                    with self.assertRaises(StateError):
                        self.poll(5)
                with self.assertRaises(StateError):
                    self.state.telegram_ingress.read("hub")
                with self.assertRaises(StateError):
                    self.state.telegram_ingress.register(
                        "hub",
                        instance_token=self.owner.instance_token,
                        previous_epoch=0,
                        now=self.now,
                    )
                self.assertEqual(self.snapshot(), before)
                self.assertIsNone(self.watermark())
                with self.state._immediate_transaction():
                    self.state._connection.execute(
                        f"UPDATE telegram_group_ingress SET {column}=?",
                        (1 if column == "epoch" else 3,),
                    )
                    self.state._connection.execute(trigger[0])


if __name__ == "__main__":
    unittest.main()
