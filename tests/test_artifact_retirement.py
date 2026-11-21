"""Receipt-bound artifact retirement is durable, exact, and fail-closed."""

from __future__ import annotations

import hashlib
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router import artifact_capabilities, migrations
from hermes_codex_router.artifacts import artifact_spool_root, remove_spooled_artifact
from hermes_codex_router.observed_delivery import archive_recovery_notice
from hermes_codex_router.state import HubState
from tests import test_outbox_sender as fixtures
from tests.schema_fixtures import project_historical_database


class ReceiptBot(fixtures.Bot):
    """Fictional Telegram transport that can also return an ambiguous receipt."""

    receipt: object = 71

    def send_html(self, *args: Any, **kwargs: Any) -> int:
        super().send_html(*args, **kwargs)
        return cast(int, self.receipt)

    def send_document(self, *args: Any, **kwargs: Any) -> int:
        super().send_document(*args, **kwargs)
        return cast(int, self.receipt)


class ArtifactRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramOutboxSenderTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        # The candidate artifact enables cleanup; the distinct schema-58
        # rollback artifact tested below retains every spool file instead.
        cleanup_capability = patch.object(
            artifact_capabilities,
            "RECEIPT_BOUND_ARTIFACT_CLEANUP",
            True,
            create=True,
        )
        cleanup_capability.start()
        self.addCleanup(cleanup_capability.stop)

    def _document_part(self, state: HubState, job_id: str) -> tuple[str, Path, bytes]:
        outbox = state.get_telegram_outbox_for_job(job_id)
        root = artifact_spool_root(self.fixture.config.state_path)
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"artifact-{job_id}.md"
        data = b"receipt-bound example artifact\n"
        path.write_bytes(data)
        path.chmod(0o600)
        with state._connection:
            state._connection.execute(
                """UPDATE telegram_outbox_parts SET part_type='document', file_path=?,
                   file_name='example.md', file_size=?, file_sha256=?
                   WHERE outbox_id=? AND part_index=1""",
                (str(path), len(data), hashlib.sha256(data).hexdigest(), outbox.outbox_id),
            )
        return outbox.outbox_id, path, data

    def _append_document_part(
        self, state: HubState, outbox_id: str, *, part_index: int = 2
    ) -> tuple[Path, bytes]:
        root = artifact_spool_root(self.fixture.config.state_path)
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"artifact-{outbox_id}-{part_index}.md"
        data = b"later receipt-bound example artifact\n"
        path.write_bytes(data)
        path.chmod(0o600)
        with state._connection:
            state._connection.execute(
                """INSERT INTO telegram_outbox_parts
                   (outbox_id,part_index,telegram_html,part_type,file_path,file_name,
                    file_size,file_sha256,receipt_validation_version)
                   VALUES (?,?,'later document','document',?,'later.md',?,?,0)""",
                (
                    outbox_id,
                    part_index,
                    str(path),
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                ),
            )
        return path, data

    @staticmethod
    def _retirements(connection: sqlite3.Connection, outbox_id: str) -> list[sqlite3.Row]:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """SELECT outbox_id,part_index,attempt_id,binding_hash,telegram_message_id,
                      delivered_at,file_path,file_name,file_size,file_sha256,retired_at
               FROM telegram_artifact_retirements WHERE outbox_id=? ORDER BY part_index""",
            (outbox_id,),
        ).fetchall()

    def _project_schema57(self, source: Path, name: str) -> Path:
        historical = source.with_name(name)
        project_historical_database(source, historical, 57)
        return historical

    def test_terminal_document_tombstone_commits_before_cleanup(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 801)
        bot = ReceiptBot()
        sender = self.fixture.sender(opencode=bot, antigravity=fixtures.Bot())
        try:
            outbox_id, path, data = self._document_part(sender.state, job_id)
            observed: list[sqlite3.Row] = []

            def cleanup(file_path: Path, spool_root: Path) -> None:
                observed.extend(self._retirements(sender.state._connection, outbox_id))
                remove_spooled_artifact(file_path, spool_root)

            with patch(
                "hermes_codex_router.final_delivery.remove_spooled_artifact", side_effect=cleanup
            ) as removed:
                self.assertTrue(sender._deliver_one("opencode"))
            removed.assert_called_once()
            self.assertFalse(path.exists())
            self.assertEqual(sender.state.get_telegram_outbox(outbox_id).status, "delivered")
            self.assertEqual(len(observed), 1)
            retired = observed[0]
            self.assertEqual(retired["outbox_id"], outbox_id)
            self.assertEqual(retired["part_index"], 1)
            self.assertEqual(retired["telegram_message_id"], 71)
            self.assertEqual(retired["file_path"], str(path))
            self.assertEqual(retired["file_size"], len(data))
            self.assertEqual(retired["file_sha256"], hashlib.sha256(data).hexdigest())
            self.assertTrue(retired["attempt_id"])
            self.assertTrue(retired["binding_hash"])
            self.assertTrue(retired["delivered_at"])
            self.assertTrue(retired["retired_at"])
        finally:
            sender.close()

    def test_rollback_artifact_keeps_receipted_document_after_retirement(self) -> None:
        """A schema-compatible rollback never deletes a retained spool file."""
        job_id = self.fixture.ready_outbox("opencode", 815)
        sender = self.fixture.sender(opencode=ReceiptBot(), antigravity=fixtures.Bot())
        try:
            outbox_id, path, _ = self._document_part(sender.state, job_id)
            with (
                patch.object(artifact_capabilities, "RECEIPT_BOUND_ARTIFACT_CLEANUP", False),
                patch("hermes_codex_router.final_delivery.remove_spooled_artifact") as removed,
            ):
                self.assertTrue(sender._deliver_one("opencode"))
            removed.assert_not_called()
            self.assertTrue(path.exists())
            self.assertEqual(sender.state.get_telegram_outbox(outbox_id).status, "delivered")
            self.assertEqual(len(self._retirements(sender.state._connection, outbox_id)), 1)
        finally:
            sender.close()

    def test_install_target_never_sweeps_an_outbox_the_rollback_completed(self) -> None:
        """Delivering a fresh outbox never removes files of an earlier delivered one."""
        retained_job = self.fixture.ready_outbox("opencode", 816)
        sender = self.fixture.sender(opencode=ReceiptBot(), antigravity=fixtures.Bot())
        try:
            retained_outbox, retained_path, _ = self._document_part(sender.state, retained_job)
            with patch.object(artifact_capabilities, "RECEIPT_BOUND_ARTIFACT_CLEANUP", False):
                self.assertTrue(sender._deliver_one("opencode"))
            self.assertTrue(retained_path.exists())
            fresh_job = self.fixture.ready_outbox("opencode", 817)
            fresh_outbox, fresh_path, _ = self._document_part(sender.state, fresh_job)
            self.assertTrue(sender._deliver_one("opencode"))
            self.assertFalse(sender._deliver_one("opencode"))
            self.assertFalse(fresh_path.exists())
            self.assertTrue(retained_path.exists())
            for outbox_id in (retained_outbox, fresh_outbox):
                self.assertEqual(sender.state.get_telegram_outbox(outbox_id).status, "delivered")
                self.assertEqual(len(self._retirements(sender.state._connection, outbox_id)), 1)
        finally:
            sender.close()

    def test_unknown_or_partially_delivered_document_has_no_tombstone(self) -> None:
        unknown_job = self.fixture.ready_outbox("opencode", 802)
        unknown_bot = ReceiptBot()
        unknown_bot.receipt = None
        unknown_sender = self.fixture.sender(opencode=unknown_bot, antigravity=fixtures.Bot())
        try:
            unknown_outbox, unknown_path, _ = self._document_part(unknown_sender.state, unknown_job)
            self.assertTrue(unknown_sender._deliver_one("opencode"))
            self.assertEqual(
                unknown_sender.state.get_telegram_outbox(unknown_outbox).status, "unknown"
            )
            self.assertTrue(unknown_path.exists())
            self.assertEqual(
                self._retirements(unknown_sender.state._connection, unknown_outbox), []
            )
        finally:
            unknown_sender.close()

        multipart_job = self.fixture.ready_outbox(
            "opencode", 803, telegram_html="Fictional multipart result " * 200
        )
        multipart_sender = self.fixture.sender(opencode=ReceiptBot(), antigravity=fixtures.Bot())
        try:
            multipart_outbox, multipart_path, _ = self._document_part(
                multipart_sender.state, multipart_job
            )
            self.assertGreater(
                len(multipart_sender.state.get_telegram_outbox_parts(multipart_outbox)), 1
            )
            self.assertTrue(multipart_sender._deliver_one("opencode"))
            self.assertEqual(
                multipart_sender.state.get_telegram_outbox(multipart_outbox).status, "pending"
            )
            self.assertEqual(
                self._retirements(multipart_sender.state._connection, multipart_outbox), []
            )
            self.assertTrue(multipart_path.exists())
            self.assertTrue(multipart_sender._deliver_one("opencode"))
            self.assertEqual(
                multipart_sender.state.get_telegram_outbox(multipart_outbox).status, "delivered"
            )
            self.assertEqual(
                len(self._retirements(multipart_sender.state._connection, multipart_outbox)), 1
            )
            self.assertFalse(multipart_path.exists())
        finally:
            multipart_sender.close()

    def test_legacy_prefix_cannot_retire_or_clean_later_document(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 814)
        sender = self.fixture.sender(opencode=ReceiptBot(), antigravity=fixtures.Bot())
        try:
            outbox = sender.state.get_telegram_outbox_for_job(job_id)
            document_path, _ = self._append_document_part(sender.state, outbox.outbox_id)
            timestamp = datetime.now(timezone.utc).isoformat()
            with sender.state._connection:
                sender.state._connection.execute(
                    """UPDATE telegram_outbox_parts SET telegram_message_id=70, delivered_at=?,
                       receipt_validation_version=0 WHERE outbox_id=? AND part_index=1""",
                    (timestamp, outbox.outbox_id),
                )

            self.assertTrue(sender._deliver_one("opencode"))
            self.assertEqual(sender.state.get_telegram_outbox(outbox.outbox_id).status, "delivered")
            self.assertEqual(self._retirements(sender.state._connection, outbox.outbox_id), [])
            self.assertTrue(document_path.exists())
        finally:
            sender.close()

        historical = self._project_schema57(
            self.fixture.config.state_path, "artifact-legacy-prefix-schema57.db"
        )
        result = migrations.migrate_database(historical)
        self.assertEqual((result.previous_version, result.current_version), (57, 60))
        with closing(sqlite3.connect(historical)) as database:
            self.assertEqual(self._retirements(database, outbox.outbox_id), [])

    def test_terminal_multiple_documents_retire_together(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 804)
        sender = self.fixture.sender(opencode=ReceiptBot(), antigravity=fixtures.Bot())
        try:
            outbox_id, _, _ = self._document_part(sender.state, job_id)
            root = artifact_spool_root(self.fixture.config.state_path)
            second_path = root / "artifact-second.md"
            second_data = b"second receipt-bound example artifact\n"
            second_path.write_bytes(second_data)
            second_path.chmod(0o600)
            with sender.state._connection:
                sender.state._connection.execute(
                    """INSERT INTO telegram_outbox_parts
                       (outbox_id,part_index,telegram_html,part_type,file_path,file_name,
                        file_size,file_sha256,receipt_validation_version)
                       VALUES (?,2,'second document','document',?,'second.md',?,?,0)""",
                    (
                        outbox_id,
                        str(second_path),
                        len(second_data),
                        hashlib.sha256(second_data).hexdigest(),
                    ),
                )

            self.assertTrue(sender._deliver_one("opencode"))
            self.assertEqual(sender.state.get_telegram_outbox(outbox_id).status, "pending")
            self.assertEqual(self._retirements(sender.state._connection, outbox_id), [])

            self.assertTrue(sender._deliver_one("opencode"))
            self.assertEqual(sender.state.get_telegram_outbox(outbox_id).status, "delivered")
            self.assertEqual(
                [
                    row["part_index"]
                    for row in self._retirements(sender.state._connection, outbox_id)
                ],
                [1, 2],
            )
        finally:
            sender.close()

    def test_cleanup_failure_keeps_terminal_retirement(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 805)
        sender = self.fixture.sender(opencode=ReceiptBot(), antigravity=fixtures.Bot())
        try:
            outbox_id, path, _ = self._document_part(sender.state, job_id)
            with patch(
                "hermes_codex_router.final_delivery.remove_spooled_artifact",
                side_effect=OSError("injected cleanup fault"),
            ):
                self.assertTrue(sender._deliver_one("opencode"))
            self.assertTrue(path.exists())
            self.assertEqual(sender.state.get_telegram_outbox(outbox_id).status, "delivered")
            self.assertEqual(len(self._retirements(sender.state._connection, outbox_id)), 1)
        finally:
            sender.close()

    def test_late_receipt_projects_retirement_and_survives_restart(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 804)
        now = datetime.now(timezone.utc) + timedelta(seconds=1)
        later = now + timedelta(minutes=4)
        with closing(
            HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        ) as state:
            outbox_id, _, _ = self._document_part(state, job_id)
            outbox = state.lease_telegram_outbox("opencode", "example-sender", now=now)
            assert outbox is not None and outbox.lease_token is not None
            state.delivery.begin_outbox_send(outbox_id, outbox.lease_token, 1, now=now)
            state.recover_stale_telegram_outbox(now=later)
            state.delivery.mark_outbox_delivered(
                outbox_id,
                outbox.lease_token,
                part_index=1,
                telegram_message_id=71,
                now=later,
            )
            self.assertEqual(len(self._retirements(state._connection, outbox_id)), 1)
        with closing(HubState.open_existing(self.fixture.config.state_path)) as restarted:
            self.assertEqual(restarted.get_telegram_outbox(outbox_id).status, "delivered")
            self.assertEqual(len(self._retirements(restarted._connection, outbox_id)), 1)

    def test_archive_may_remove_live_outbox_but_retains_tombstone(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 805)
        sender = self.fixture.sender(opencode=ReceiptBot(), antigravity=fixtures.Bot())
        try:
            outbox_id, _, _ = self._document_part(sender.state, job_id)
            self.assertTrue(sender._deliver_one("opencode"))
            with sender.state._immediate_transaction():
                archive_recovery_notice(
                    sender.state._connection,
                    job_id=job_id,
                    outbox_id=outbox_id,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                sender.state._connection.execute(
                    "DELETE FROM telegram_outbox_parts WHERE outbox_id=?", (outbox_id,)
                )
                sender.state._connection.execute(
                    "DELETE FROM telegram_outbox WHERE outbox_id=?", (outbox_id,)
                )
            self.assertEqual(len(self._retirements(sender.state._connection, outbox_id)), 1)
        finally:
            sender.close()

    def test_migration_backfills_only_exact_v1_receipt_evidence(self) -> None:
        exact_job = self.fixture.ready_outbox("opencode", 806)
        sender = self.fixture.sender(opencode=ReceiptBot(), antigravity=fixtures.Bot())
        try:
            exact_outbox, _, _ = self._document_part(sender.state, exact_job)
            self.assertTrue(sender._deliver_one("opencode"))
        finally:
            sender.close()

        legacy_job = self.fixture.ready_outbox("opencode", 807)
        incomplete_job = self.fixture.ready_outbox("opencode", 808)
        mismatched_job = self.fixture.ready_outbox("opencode", 809)
        with closing(
            HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        ) as state:
            legacy_outbox, _, _ = self._document_part(state, legacy_job)
            incomplete_outbox, _, _ = self._document_part(state, incomplete_job)
            mismatched_outbox, _, _ = self._document_part(state, mismatched_job)
            timestamp = datetime.now(timezone.utc).isoformat()
            with state._connection:
                for outbox_id, receipt_version in (
                    (legacy_outbox, 0),
                    (incomplete_outbox, 1),
                ):
                    state._connection.execute(
                        """UPDATE telegram_outbox SET status='delivered', telegram_message_id=71,
                           delivered_at=?, updated_at=? WHERE outbox_id=?""",
                        (timestamp, timestamp, outbox_id),
                    )
                    state._connection.execute(
                        """UPDATE telegram_outbox_parts SET telegram_message_id=71,
                           delivered_at=?, receipt_validation_version=?
                           WHERE outbox_id=? AND part_index=1""",
                        (timestamp, receipt_version, outbox_id),
                    )
            with state._connection:
                state._connection.execute(
                    """UPDATE telegram_outbox SET status='delivered', telegram_message_id=71,
                       delivered_at=?, updated_at=? WHERE outbox_id=?""",
                    (timestamp, timestamp, mismatched_outbox),
                )
                state._connection.execute(
                    """UPDATE telegram_outbox_parts SET telegram_message_id=71, delivered_at=?,
                       receipt_validation_version=1 WHERE outbox_id=? AND part_index=1""",
                    (timestamp, mismatched_outbox),
                )
                state._connection.execute(
                    """INSERT INTO telegram_send_attempts
                       (attempt_id,target_kind,target_id,part_index,binding_hash,started_at)
                       VALUES ('mismatched-attempt','final',?,1,?,?)""",
                    (mismatched_outbox, "b" * 64, timestamp),
                )
                state._connection.execute(
                    """INSERT INTO telegram_send_receipts
                       (attempt_id,telegram_message_id,recorded_at)
                       VALUES ('mismatched-attempt',71,?)""",
                    (timestamp,),
                )

        historical = self._project_schema57(self.fixture.config.state_path, "artifact-schema57.db")
        result = migrations.migrate_database(historical)
        self.assertEqual((result.previous_version, result.current_version), (57, 60))
        with closing(sqlite3.connect(historical)) as database:
            self.assertEqual(len(self._retirements(database, exact_outbox)), 1)
            self.assertEqual(self._retirements(database, legacy_outbox), [])
            self.assertEqual(self._retirements(database, incomplete_outbox), [])
            self.assertEqual(self._retirements(database, mismatched_outbox), [])

    def test_migration_leaves_mutated_v1_document_unretired(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 813)
        sender = self.fixture.sender(opencode=ReceiptBot(), antigravity=fixtures.Bot())
        try:
            outbox_id, _, _ = self._document_part(sender.state, job_id)
            self.assertTrue(sender._deliver_one("opencode"))
        finally:
            sender.close()

        historical = self._project_schema57(
            self.fixture.config.state_path, "artifact-malformed-schema57.db"
        )
        with closing(sqlite3.connect(historical)) as database, database:
            trigger = database.execute(
                """SELECT sql FROM sqlite_master
                   WHERE type='trigger' AND name='telegram_outbox_parts_artifact_update'"""
            ).fetchone()
            assert trigger is not None and trigger[0] is not None
            database.execute("DROP TRIGGER telegram_outbox_parts_artifact_update")
            database.execute(
                "UPDATE telegram_outbox_parts SET file_size=NULL WHERE outbox_id=? AND part_index=1",
                (outbox_id,),
            )
            database.execute(str(trigger[0]))

        result = migrations.migrate_database(historical)
        self.assertEqual((result.previous_version, result.current_version), (57, 60))
        with closing(sqlite3.connect(historical)) as database:
            self.assertEqual(self._retirements(database, outbox_id), [])

    def test_tombstone_refuses_mismatched_snapshot_and_freezes_source(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 810)
        timestamp = datetime.now(timezone.utc).isoformat()
        with closing(
            HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        ) as state:
            outbox_id, path, data = self._document_part(state, job_id)
            with state._connection:
                state._connection.execute(
                    """UPDATE telegram_outbox SET status='delivered', telegram_message_id=71,
                       delivered_at=?, updated_at=? WHERE outbox_id=?""",
                    (timestamp, timestamp, outbox_id),
                )
                state._connection.execute(
                    """UPDATE telegram_outbox_parts SET telegram_message_id=71, delivered_at=?,
                       receipt_validation_version=1 WHERE outbox_id=? AND part_index=1""",
                    (timestamp, outbox_id),
                )
                binding = state.delivery._receipts._binding("final", outbox_id, 1)
                assert binding is not None
                state._connection.execute(
                    """INSERT INTO telegram_send_attempts
                       (attempt_id,target_kind,target_id,part_index,binding_hash,started_at)
                       VALUES ('exact-attempt','final',?,1,?,?)""",
                    (outbox_id, binding, timestamp),
                )
                state._connection.execute(
                    """INSERT INTO telegram_send_receipts
                       (attempt_id,telegram_message_id,recorded_at) VALUES ('exact-attempt',71,?)""",
                    (timestamp,),
                )

            values = {
                "outbox_id": outbox_id,
                "part_index": 1,
                "attempt_id": "exact-attempt",
                "binding_hash": binding,
                "telegram_message_id": 71,
                "delivered_at": timestamp,
                "file_path": str(path),
                "file_name": "example.md",
                "file_size": len(data),
                "file_sha256": hashlib.sha256(data).hexdigest(),
                "retired_at": timestamp,
            }

            def insert(**changes: object) -> None:
                row = {**values, **changes}
                state._connection.execute(
                    """INSERT INTO telegram_artifact_retirements
                       (outbox_id,part_index,attempt_id,binding_hash,telegram_message_id,delivered_at,
                        file_path,file_name,file_size,file_sha256,retired_at)
                       VALUES (:outbox_id,:part_index,:attempt_id,:binding_hash,:telegram_message_id,
                               :delivered_at,:file_path,:file_name,:file_size,:file_sha256,:retired_at)""",
                    row,
                )

            for changed in (
                {"attempt_id": "missing-attempt"},
                {"binding_hash": "b" * 64},
                {"telegram_message_id": 72},
                {"delivered_at": "2099-01-01T00:00:00+00:00"},
                {"file_path": str(path) + ".changed"},
                {"file_sha256": "b" * 64},
            ):
                with self.subTest(changed=changed), self.assertRaises(sqlite3.IntegrityError):
                    with state._connection:
                        insert(**changed)

            with state._connection:
                insert()
            self.assertEqual(len(self._retirements(state._connection, outbox_id)), 1)
            for sql in (
                "UPDATE telegram_artifact_retirements SET retired_at='2099-01-01T00:00:00+00:00'",
                "DELETE FROM telegram_artifact_retirements",
                "INSERT OR REPLACE INTO telegram_artifact_retirements "
                "SELECT * FROM telegram_artifact_retirements",
                "UPDATE telegram_outbox_parts SET telegram_html='changed'",
                "UPDATE telegram_outbox_parts SET file_path='/tmp/changed'",
                "UPDATE telegram_outbox SET chat_id=999",
            ):
                with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                    with state._connection:
                        state._connection.execute(sql)

    def test_migration_fault_rolls_back_tombstone_ddl_and_version(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 811)
        with closing(
            HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        ) as state:
            self._document_part(state, job_id)
        historical = self._project_schema57(
            self.fixture.config.state_path, "artifact-fault-schema57.db"
        )
        original = migrations._execute_migration_script

        def fault(connection: sqlite3.Connection, script: str) -> None:
            original(connection, script)
            if script == migrations.MIGRATION_58:
                raise sqlite3.OperationalError("injected after schema58 DDL")

        with patch.object(migrations, "_execute_migration_script", side_effect=fault):
            with self.assertRaises(sqlite3.OperationalError):
                migrations.migrate_database(historical)
        with closing(sqlite3.connect(historical)) as database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 57)
            self.assertEqual(
                database.execute(
                    "SELECT name FROM sqlite_master WHERE name='telegram_artifact_retirements'"
                ).fetchall(),
                [],
            )
            self.assertEqual(database.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_backfill_fault_rolls_back_tombstone_ddl_and_version(self) -> None:
        job_id = self.fixture.ready_outbox("opencode", 812)
        with closing(
            HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        ) as state:
            self._document_part(state, job_id)
        historical = self._project_schema57(
            self.fixture.config.state_path, "artifact-backfill-fault-schema57.db"
        )
        with (
            patch.object(
                migrations,
                "backfill_artifact_retirements",
                side_effect=sqlite3.OperationalError("injected schema58 backfill fault"),
            ),
            self.assertRaises(sqlite3.OperationalError),
        ):
            migrations.migrate_database(historical)
        with closing(sqlite3.connect(historical)) as database:
            self.assertEqual(database.execute("PRAGMA user_version").fetchone()[0], 57)
            self.assertEqual(
                database.execute(
                    "SELECT name FROM sqlite_master WHERE name='telegram_artifact_retirements'"
                ).fetchall(),
                [],
            )


if __name__ == "__main__":
    unittest.main()
