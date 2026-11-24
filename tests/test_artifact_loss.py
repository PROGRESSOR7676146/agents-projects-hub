"""Explicit owner-attested attachment loss; all fixtures are local and fictional."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import threading
import unittest
import uuid
from contextlib import closing, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import artifact_loss_cli, migrations
from hermes_codex_router.artifact_loss_state import (
    _AUTHORITY,
    _GENESIS,
    _REASON,
    ArtifactLossPreview,
    ArtifactLossState,
    ArtifactLossTarget,
    _digest,
)
from hermes_codex_router.artifact_recovery_inventory import (
    ArtifactRecoveryInventoryError,
    inspect_artifact_recovery_inventory,
)
from hermes_codex_router.artifacts import artifact_spool_root, spool_staged_artifacts
from hermes_codex_router.cli import main
from hermes_codex_router.database_backup import backup_database
from hermes_codex_router.delivery_receipts import DeliveryReceipts
from hermes_codex_router.observed_delivery import archive_recovery_notice
from hermes_codex_router.schema_artifact_losses import ARTIFACT_LOSS_SCHEMA_OBJECTS
from hermes_codex_router.state import HubState, StateError
from tests.schema_fixtures import project_historical_database


class ArtifactLossTests(unittest.TestCase):
    def setUp(self) -> None:
        # These tests exercise the install-target artifact, which permits new
        # declarations; the distinct schema-compatible rollback artifact refuses
        # them and is covered by the explicit refusal test below.
        declaration_capability = patch(
            "hermes_codex_router.artifact_capabilities.EXPLICIT_ARTIFACT_LOSS_DECLARATION",
            True,
        )
        declaration_capability.start()
        self.addCleanup(declaration_capability.stop)
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.state_path = self.root / "state" / "hub.db"
        self.project = self.root / "project"
        self.project.mkdir()
        self.spool = artifact_spool_root(self.state_path)
        self.state = HubState.open(self.state_path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.target = self._missing_document_target()

    def _snapshot(self, name: str) -> Path:
        return backup_database(self.state_path, self.root / name)

    def _declare(self, *, audit: str = "a" * 64):
        preview = self.state.preview_artifact_loss(
            (self.target,), spool_root=self.spool, recovery_audit_sha256=audit
        )
        return self.state.declare_artifact_loss(
            (self.target,),
            spool_root=self.spool,
            recovery_audit_sha256=audit,
            expected_snapshot=preview.snapshot,
            operation_id=preview.operation_id,
            acknowledge_irrecoverable_loss=True,
            now=datetime(2026, 11, 19, tzinfo=timezone.utc),
        )

    def _target_path(self) -> Path:
        row = self.state._connection.execute(
            "SELECT file_path FROM telegram_outbox_parts WHERE outbox_id=? AND part_index=?",
            (self.target.outbox_id, self.target.part_index),
        ).fetchone()
        assert row is not None and isinstance(row[0], str)
        return Path(row[0])

    def _append_missing_document_part(self) -> ArtifactLossTarget:
        part_index = self.state._connection.execute(
            "SELECT MAX(part_index) + 1 FROM telegram_outbox_parts WHERE outbox_id=?",
            (self.target.outbox_id,),
        ).fetchone()[0]
        assert type(part_index) is int
        path = self.spool / "fictional-extra" / "missing.md"
        with self.state._connection:
            self.state._connection.execute(
                """INSERT INTO telegram_outbox_parts
                   (outbox_id,part_index,telegram_html,part_type,file_path,file_name,file_size,file_sha256)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (
                    self.target.outbox_id,
                    part_index,
                    "fictional extra document",
                    "document",
                    str(path),
                    "missing.md",
                    7,
                    "d" * 64,
                ),
            )
        return ArtifactLossTarget(self.target.outbox_id, part_index)

    def _missing_document_target(self) -> ArtifactLossTarget:
        staging = self.project / ".hub" / "staging" / "fictional-job"
        staging.mkdir(parents=True)
        (staging / "report.md").write_text("fictional report\n", encoding="utf-8")
        artifact = spool_staged_artifacts(self.project, "fictional-job", self.spool)[0]
        topic = self.state.observe_topic(
            project_id="example-project", chat_id=-1001234567890, thread_id=70, title="Example"
        )
        session = self.state.activate_agent(topic.topic_id, "codex", "example-model", "high")
        job, _ = self.state.enqueue_provider_job(
            idempotency_key="telegram:-1001234567890:70",
            chat_id=-1001234567890,
            message_id=70,
            topic_id=topic.topic_id,
            agent_id="codex",
            session_id=session.session_id,
            session_generation=session.generation,
            provider_session_id=None,
            model=session.model,
            effort=session.effort,
            payload_text="fictional task",
            context_watermark=None,
            handoff_id=None,
        )
        lease = self.state.lease_provider_job("codex", "fictional-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, lease.lease_token)
        self.state.commit_provider_result(
            job.job_id,
            lease.lease_token,
            visible_response="fictional result",
            telegram_html="fictional result",
            sender_agent_id="codex",
            artifacts=(artifact,),
        )
        outbox = self.state.get_telegram_outbox_for_job(job.job_id)
        part = next(
            part
            for part in self.state.delivery.get_outbox_parts(outbox.outbox_id)
            if part.part_type == "document"
        )
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE telegram_outbox SET status='unknown',lease_owner=NULL,lease_token=NULL,"
                "lease_expires_at=NULL WHERE outbox_id=?",
                (outbox.outbox_id,),
            )
        artifact.path.unlink()
        return ArtifactLossTarget(outbox.outbox_id, part.part_index)

    def test_preview_and_apply_append_only_loss_without_mutating_delivery(self) -> None:
        before = self.state._connection.execute(
            "SELECT status,telegram_message_id,delivered_at FROM telegram_outbox WHERE outbox_id=?",
            (self.target.outbox_id,),
        ).fetchone()
        preview = self.state.preview_artifact_loss(
            (self.target,), spool_root=self.spool, recovery_audit_sha256="a" * 64
        )
        self.assertEqual(preview.target_count, 1)
        record = self.state.declare_artifact_loss(
            (self.target,),
            spool_root=self.spool,
            recovery_audit_sha256="a" * 64,
            expected_snapshot=preview.snapshot,
            operation_id=preview.operation_id,
            acknowledge_irrecoverable_loss=True,
            now=datetime(2026, 11, 19, tzinfo=timezone.utc),
        )
        self.assertEqual(record.target_count, 1)
        self.assertEqual(
            self.state.declare_artifact_loss(
                (self.target,),
                spool_root=self.spool,
                recovery_audit_sha256="a" * 64,
                expected_snapshot=preview.snapshot,
                operation_id=preview.operation_id,
                acknowledge_irrecoverable_loss=True,
                now=datetime(2026, 11, 19, tzinfo=timezone.utc),
            ),
            record,
        )
        after = self.state._connection.execute(
            "SELECT status,telegram_message_id,delivered_at FROM telegram_outbox WHERE outbox_id=?",
            (self.target.outbox_id,),
        ).fetchone()
        self.assertEqual(after, before)
        inventory = inspect_artifact_recovery_inventory(
            backup_database(self.state_path, self.root / "artifact-inventory.db"), self.spool
        )
        self.assertEqual(inventory.coverage, "degraded_explicit_loss")
        self.assertEqual(inventory.entries[0].availability, "explicit_loss")

    def test_rollback_artifact_capability_refuses_new_loss_declarations(self) -> None:
        """A schema-compatible rollback may inspect history but cannot extend it."""
        record = self._declare()
        snapshot = self._snapshot("rollback-inventory.db")
        other = self._append_missing_document_part()
        preview = self.state.preview_artifact_loss(
            (other,), spool_root=self.spool, recovery_audit_sha256="a" * 64
        )
        selection = self.root / "rollback-targets.json"
        selection.write_text(
            json.dumps(
                {
                    "version": 1,
                    "targets": [{"outbox_id": other.outbox_id, "part_index": other.part_index}],
                }
            ),
            encoding="utf-8",
        )
        selection.chmod(0o600)
        with patch(
            "hermes_codex_router.artifact_capabilities.EXPLICIT_ARTIFACT_LOSS_DECLARATION",
            False,
        ):
            with self.assertRaisesRegex(StateError, "does not permit artifact-loss declarations"):
                self.state.preview_artifact_loss(
                    (self.target,), spool_root=self.spool, recovery_audit_sha256="a" * 64
                )
            with self.assertRaisesRegex(StateError, "does not permit artifact-loss declarations"):
                self.state.declare_artifact_loss(
                    (other,),
                    spool_root=self.spool,
                    recovery_audit_sha256="a" * 64,
                    expected_snapshot=preview.snapshot,
                    operation_id=preview.operation_id,
                    acknowledge_irrecoverable_loss=True,
                )
            with self.assertRaisesRegex(StateError, "does not permit artifact-loss declarations"):
                self.state.declare_artifact_loss(
                    (self.target,),
                    spool_root=self.spool,
                    recovery_audit_sha256="a" * 64,
                    expected_snapshot=record.snapshot,
                    operation_id=record.operation_id,
                    acknowledge_irrecoverable_loss=True,
                )
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "artifact-loss",
                            "apply",
                            str(self.state_path),
                            "--spool-root",
                            str(self.spool),
                            "--targets",
                            str(selection),
                            "--recovery-audit-sha256",
                            "a" * 64,
                            "--snapshot",
                            preview.snapshot,
                            "--operation-id",
                            preview.operation_id,
                            "--acknowledge-irrecoverable-loss",
                        ]
                    ),
                    2,
                )
            self.assertIn("does not permit artifact-loss declarations", output.getvalue())

            inventory = inspect_artifact_recovery_inventory(snapshot, self.spool)
        self.assertEqual(inventory.coverage, "degraded_explicit_loss")
        self.assertEqual(inventory.ledger_sequence, record.sequence)

    def test_exact_retry_rejects_a_different_target_list(self) -> None:
        """An operation id is bound to its exact declared document set."""
        record = self._declare()
        other = self._append_missing_document_part()

        with self.assertRaisesRegex(StateError, "different immutable contents"):
            self.state.declare_artifact_loss(
                (other,),
                spool_root=self.spool,
                recovery_audit_sha256="a" * 64,
                expected_snapshot=record.snapshot,
                operation_id=record.operation_id,
                acknowledge_irrecoverable_loss=True,
                now=datetime(2026, 11, 19, tzinfo=timezone.utc),
            )

        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM artifact_loss_operations"
            ).fetchone()[0],
            1,
        )

    def test_stale_preview_and_present_file_refuse(self) -> None:
        preview = self.state.preview_artifact_loss(
            (self.target,), spool_root=self.spool, recovery_audit_sha256="b" * 64
        )
        path = self.spool / "restored.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("restored", encoding="utf-8")
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE telegram_outbox_parts SET file_path=?,file_name=?,file_size=?,file_sha256=? "
                "WHERE outbox_id=? AND part_index=?",
                (
                    str(path),
                    "restored.md",
                    8,
                    "0" * 64,
                    self.target.outbox_id,
                    self.target.part_index,
                ),
            )
        with self.assertRaises(StateError):
            self.state.declare_artifact_loss(
                (self.target,),
                spool_root=self.spool,
                recovery_audit_sha256="b" * 64,
                expected_snapshot=preview.snapshot,
                operation_id=preview.operation_id,
                acknowledge_irrecoverable_loss=True,
            )

    def test_batch_is_atomic_when_one_target_changes_after_preview(self) -> None:
        extra = self._append_missing_document_part()
        preview = self.state.preview_artifact_loss(
            (self.target, extra), spool_root=self.spool, recovery_audit_sha256="1" * 64
        )
        path = self.spool / "fictional-extra" / "missing.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("present", encoding="utf-8")
        with self.assertRaises(StateError):
            self.state.declare_artifact_loss(
                (self.target, extra),
                spool_root=self.spool,
                recovery_audit_sha256="1" * 64,
                expected_snapshot=preview.snapshot,
                operation_id=preview.operation_id,
                acknowledge_irrecoverable_loss=True,
            )
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM artifact_loss_operations"
            ).fetchone()[0],
            0,
        )

    def test_late_receipt_can_retire_after_loss_without_rewriting_history(self) -> None:
        record = self._declare()
        timestamp = "2026-11-19T00:00:00+00:00"
        with self.state._immediate_transaction():
            receipts = DeliveryReceipts(self.state._connection, StateError)
            parts = self.state._connection.execute(
                "SELECT part_index FROM telegram_outbox_parts WHERE outbox_id=? ORDER BY part_index",
                (self.target.outbox_id,),
            ).fetchall()
            for index, row in enumerate(parts, start=1):
                part_index = int(row["part_index"])
                token = f"fictional-late-receipt-{part_index}"
                message_id = 900 + index
                receipts.begin("final", self.target.outbox_id, token, part_index, timestamp)
                receipts.record(
                    "final", self.target.outbox_id, token, part_index, message_id, timestamp
                )
                self.state._connection.execute(
                    """UPDATE telegram_outbox_parts SET telegram_message_id=?,delivered_at=?,
                       receipt_validation_version=1 WHERE outbox_id=? AND part_index=?""",
                    (message_id, timestamp, self.target.outbox_id, part_index),
                )
            self.state._connection.execute(
                "UPDATE telegram_outbox SET status='delivered',delivered_at=? WHERE outbox_id=?",
                (timestamp, self.target.outbox_id),
            )
            self.assertTrue(receipts.retire_terminal_artifacts(self.target.outbox_id))
        inventory = inspect_artifact_recovery_inventory(
            self._snapshot("late-retirement.db"), self.spool
        )
        self.assertEqual(inventory.coverage, "complete_artifact_coverage")
        self.assertEqual(inventory.entries[0].availability, "receipt_retired")
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM artifact_loss_operations WHERE operation_id=?",
                (record.operation_id,),
            ).fetchone()[0],
            1,
        )

    def test_loss_ledger_and_bound_source_identity_are_immutable(self) -> None:
        self._declare()
        with self.assertRaises(sqlite3.IntegrityError), self.state._connection:
            self.state._connection.execute("DELETE FROM artifact_loss_operations")
        with self.assertRaises(sqlite3.IntegrityError), self.state._connection:
            self.state._connection.execute("UPDATE artifact_loss_items SET file_size=2")
        with self.assertRaises(sqlite3.IntegrityError), self.state._connection:
            self.state._connection.execute(
                """UPDATE telegram_outbox_parts SET file_name='changed.md'
                   WHERE outbox_id=? AND part_index=?""",
                (self.target.outbox_id, self.target.part_index),
            )

    def test_exact_archive_tombstone_retains_declared_loss_for_inventory(self) -> None:
        record = self._declare()
        connection = self.state._connection
        job_id = connection.execute(
            "SELECT job_id FROM telegram_outbox WHERE outbox_id=?", (self.target.outbox_id,)
        ).fetchone()[0]
        timestamp = "2026-11-20T00:00:00+00:00"

        with self.state._immediate_transaction():
            archive_recovery_notice(
                connection,
                job_id=job_id,
                outbox_id=self.target.outbox_id,
                timestamp=timestamp,
            )
            connection.execute(
                "DELETE FROM telegram_outbox_parts WHERE outbox_id=? AND part_index=?",
                (self.target.outbox_id, self.target.part_index),
            )
            connection.execute(
                "DELETE FROM telegram_outbox WHERE outbox_id=?", (self.target.outbox_id,)
            )

        self.assertEqual(
            ArtifactLossState(connection, StateError).ledger_head(),
            (record.sequence, record.commit_hash),
        )
        inventory = inspect_artifact_recovery_inventory(
            self._snapshot("archived-declared-loss.db"), self.spool
        )
        self.assertTrue(inventory.ledger_valid)
        self.assertEqual(inventory.coverage, "degraded_explicit_loss")
        self.assertEqual(
            [
                (entry.outbox_id, entry.part_index, entry.availability)
                for entry in inventory.entries
            ],
            [(self.target.outbox_id, self.target.part_index, "explicit_loss")],
        )

        target_path = Path(
            connection.execute(
                """SELECT source_file_path FROM artifact_loss_archive_tombstones
                   WHERE outbox_id=? AND part_index=?""",
                (self.target.outbox_id, self.target.part_index),
            ).fetchone()[0]
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("fictional report\n", encoding="utf-8")
        reappeared = inspect_artifact_recovery_inventory(
            self._snapshot("archived-loss-reappeared.db"), self.spool
        )
        self.assertEqual(reappeared.entries[0].availability, "present_required")

    def test_declared_loss_archive_tombstone_and_archive_are_immutable(self) -> None:
        self._declare()
        connection = self.state._connection
        job_id = connection.execute(
            "SELECT job_id FROM telegram_outbox WHERE outbox_id=?", (self.target.outbox_id,)
        ).fetchone()[0]
        with self.state._immediate_transaction():
            archive_recovery_notice(
                connection,
                job_id=job_id,
                outbox_id=self.target.outbox_id,
                timestamp="2026-11-20T00:00:00+00:00",
            )
            connection.execute(
                "DELETE FROM telegram_outbox_parts WHERE outbox_id=? AND part_index=?",
                (self.target.outbox_id, self.target.part_index),
            )
            connection.execute(
                "DELETE FROM telegram_outbox WHERE outbox_id=?", (self.target.outbox_id,)
            )

        for statement, params in (
            (
                "UPDATE artifact_loss_archive_tombstones SET archive_saved_at=? "
                "WHERE outbox_id=? AND part_index=?",
                ("changed", self.target.outbox_id, self.target.part_index),
            ),
            (
                "DELETE FROM artifact_loss_archive_tombstones WHERE outbox_id=? AND part_index=?",
                (self.target.outbox_id, self.target.part_index),
            ),
            (
                "UPDATE provider_recovery_notice_parts SET file_name='changed.md' WHERE job_id=? "
                "AND part_index=?",
                (job_id, self.target.part_index),
            ),
        ):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                with connection:
                    connection.execute(statement, params)

        connection.execute("DROP TRIGGER artifact_loss_archive_part_immutable")
        try:
            with connection:
                connection.execute(
                    """UPDATE provider_recovery_notice_parts SET file_name='corrupted.md'
                       WHERE job_id=? AND part_index=?""",
                    (job_id, self.target.part_index),
                )
        finally:
            connection.execute(
                ARTIFACT_LOSS_SCHEMA_OBJECTS[("trigger", "artifact_loss_archive_part_immutable")]
            )
        corrupted = inspect_artifact_recovery_inventory(
            self._snapshot("corrupted-archived-loss.db"), self.spool
        )
        self.assertFalse(corrupted.ledger_valid)
        self.assertEqual(corrupted.coverage, "incomplete")

    def test_declared_loss_archive_rejects_a_mismatched_copy(self) -> None:
        self._declare()
        connection = self.state._connection
        job_id = connection.execute(
            "SELECT job_id FROM telegram_outbox WHERE outbox_id=?", (self.target.outbox_id,)
        ).fetchone()[0]
        connection.execute(
            """CREATE TRIGGER fixture_loss_archive_mismatch
               AFTER INSERT ON provider_recovery_notice_parts
               BEGIN
                   UPDATE provider_recovery_notice_parts SET file_name='mismatch.md'
                   WHERE job_id=NEW.job_id AND part_index=NEW.part_index;
               END"""
        )
        try:
            with self.assertRaisesRegex(RuntimeError, "lacks an exact copy"):
                with self.state._immediate_transaction():
                    archive_recovery_notice(
                        connection,
                        job_id=job_id,
                        outbox_id=self.target.outbox_id,
                        timestamp="2026-11-20T00:00:00+00:00",
                    )
        finally:
            connection.execute("DROP TRIGGER fixture_loss_archive_mismatch")
        self.assertIsNotNone(
            connection.execute(
                "SELECT 1 FROM telegram_outbox_parts WHERE outbox_id=? AND part_index=?",
                (self.target.outbox_id, self.target.part_index),
            ).fetchone()
        )
        self.assertIsNone(
            connection.execute(
                "SELECT 1 FROM artifact_loss_archive_tombstones WHERE outbox_id=? AND part_index=?",
                (self.target.outbox_id, self.target.part_index),
            ).fetchone()
        )

    def test_archive_cannot_remove_an_outbox_until_every_declared_part_is_tombstoned(self) -> None:
        extra = self._append_missing_document_part()
        preview = self.state.preview_artifact_loss(
            (self.target, extra), spool_root=self.spool, recovery_audit_sha256="a" * 64
        )
        self.state.declare_artifact_loss(
            (self.target, extra),
            spool_root=self.spool,
            recovery_audit_sha256="a" * 64,
            expected_snapshot=preview.snapshot,
            operation_id=preview.operation_id,
            acknowledge_irrecoverable_loss=True,
            now=datetime(2026, 11, 20, tzinfo=timezone.utc),
        )
        connection = self.state._connection
        job_id = connection.execute(
            "SELECT job_id FROM telegram_outbox WHERE outbox_id=?", (self.target.outbox_id,)
        ).fetchone()[0]
        with self.state._immediate_transaction():
            archive_recovery_notice(
                connection,
                job_id=job_id,
                outbox_id=self.target.outbox_id,
                timestamp="2026-11-20T00:00:00+00:00",
            )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM artifact_loss_archive_tombstones WHERE outbox_id=?",
                (self.target.outbox_id,),
            ).fetchone()[0],
            2,
        )
        connection.execute("DROP TRIGGER artifact_loss_archive_tombstones_no_delete")
        try:
            with connection:
                connection.execute(
                    "DELETE FROM artifact_loss_archive_tombstones "
                    "WHERE outbox_id=? AND part_index=?",
                    (extra.outbox_id, extra.part_index),
                )
        finally:
            connection.execute(
                ARTIFACT_LOSS_SCHEMA_OBJECTS[
                    ("trigger", "artifact_loss_archive_tombstones_no_delete")
                ]
            )
        with self.assertRaises(sqlite3.IntegrityError), connection:
            connection.execute(
                "DELETE FROM telegram_outbox WHERE outbox_id=?", (self.target.outbox_id,)
            )

    def test_source_tombstone_conflict_and_missing_tombstone_fail_closed(self) -> None:
        self._declare()
        connection = self.state._connection
        job_id = connection.execute(
            "SELECT job_id FROM telegram_outbox WHERE outbox_id=?", (self.target.outbox_id,)
        ).fetchone()[0]
        with self.state._immediate_transaction():
            archive_recovery_notice(
                connection,
                job_id=job_id,
                outbox_id=self.target.outbox_id,
                timestamp="2026-11-20T00:00:00+00:00",
            )
        with self.assertRaisesRegex(StateError, "conflicts with its archive tombstone"):
            ArtifactLossState(connection, StateError).ledger_head()
        conflict = inspect_artifact_recovery_inventory(
            self._snapshot("source-tombstone-conflict.db"), self.spool
        )
        self.assertFalse(conflict.ledger_valid)
        self.assertEqual(len(conflict.entries), 1)

        connection.execute("DROP TRIGGER artifact_loss_archive_tombstones_no_delete")
        with connection:
            connection.execute(
                "DELETE FROM artifact_loss_archive_tombstones WHERE outbox_id=? AND part_index=?",
                (self.target.outbox_id, self.target.part_index),
            )
        connection.execute(
            ARTIFACT_LOSS_SCHEMA_OBJECTS[("trigger", "artifact_loss_archive_tombstones_no_delete")]
        )
        connection.execute("DROP TRIGGER artifact_loss_source_part_no_delete")
        with connection:
            connection.execute(
                "DELETE FROM telegram_outbox_parts WHERE outbox_id=? AND part_index=?",
                (self.target.outbox_id, self.target.part_index),
            )
        connection.execute(
            ARTIFACT_LOSS_SCHEMA_OBJECTS[("trigger", "artifact_loss_source_part_no_delete")]
        )
        missing = inspect_artifact_recovery_inventory(
            self._snapshot("missing-tombstone-source.db"), self.spool
        )
        self.assertFalse(missing.ledger_valid)
        self.assertEqual(len(missing.entries), 1)
        self.assertEqual(missing.entries[0].availability, "unresolved_missing")

    def test_loss_ledger_freezes_all_binding_sources_but_allows_late_receipts(self) -> None:
        self._declare()
        connection = self.state._connection
        job_id = connection.execute(
            "SELECT job_id FROM telegram_outbox WHERE outbox_id=?", (self.target.outbox_id,)
        ).fetchone()[0]
        topic_id = connection.execute(
            "SELECT topic_id FROM provider_jobs WHERE job_id=?", (job_id,)
        ).fetchone()[0]

        for statement, params in (
            (
                "UPDATE telegram_outbox_parts SET telegram_html='changed' "
                "WHERE outbox_id=? AND part_index=?",
                (self.target.outbox_id, self.target.part_index),
            ),
            (
                "UPDATE telegram_outbox SET telegram_html='changed' WHERE outbox_id=?",
                (self.target.outbox_id,),
            ),
            ("UPDATE provider_jobs SET agent_id='other' WHERE job_id=?", (job_id,)),
            ("UPDATE topics SET project_id='other' WHERE topic_id=?", (topic_id,)),
        ):
            with (
                self.subTest(statement=statement),
                self.assertRaises(sqlite3.IntegrityError),
                connection,
            ):
                connection.execute(statement, params)

        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            for statement, params in (
                (
                    "DELETE FROM telegram_outbox_parts WHERE outbox_id=? AND part_index=?",
                    (self.target.outbox_id, self.target.part_index),
                ),
                ("DELETE FROM telegram_outbox WHERE outbox_id=?", (self.target.outbox_id,)),
                ("DELETE FROM provider_jobs WHERE job_id=?", (job_id,)),
                ("DELETE FROM topics WHERE topic_id=?", (topic_id,)),
            ):
                with (
                    self.subTest(statement=statement),
                    self.assertRaises(sqlite3.IntegrityError),
                    connection,
                ):
                    connection.execute(statement, params)
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

        with connection:
            connection.execute(
                """UPDATE telegram_outbox_parts SET telegram_message_id=?,delivered_at=?,
                   receipt_validation_version=1 WHERE outbox_id=? AND part_index=?""",
                (900, "2026-11-19T00:00:00+00:00", self.target.outbox_id, self.target.part_index),
            )
            connection.execute(
                "UPDATE telegram_outbox SET status='delivered',delivered_at=? WHERE outbox_id=?",
                ("2026-11-19T00:00:00+00:00", self.target.outbox_id),
            )

        part = connection.execute(
            "SELECT * FROM telegram_outbox_parts WHERE outbox_id=? AND part_index=?",
            (self.target.outbox_id, self.target.part_index),
        ).fetchone()
        outbox = connection.execute(
            "SELECT * FROM telegram_outbox WHERE outbox_id=?", (self.target.outbox_id,)
        ).fetchone()
        assert part is not None and outbox is not None
        connection.execute("DROP TRIGGER artifact_loss_source_part_no_delete")
        with connection:
            connection.execute(
                "DELETE FROM telegram_outbox_parts WHERE outbox_id=? AND part_index=?",
                (self.target.outbox_id, self.target.part_index),
            )
        part_columns = tuple(part.keys())
        with self.assertRaises(sqlite3.IntegrityError), connection:
            connection.execute(
                "INSERT INTO telegram_outbox_parts ("
                + ",".join(part_columns)
                + ") VALUES ("
                + ",".join("?" for _ in part_columns)
                + ")",
                tuple(part[column] for column in part_columns),
            )

        connection.execute("DROP TRIGGER artifact_loss_source_outbox_no_delete")
        with connection:
            connection.execute(
                "DELETE FROM telegram_outbox WHERE outbox_id=?", (self.target.outbox_id,)
            )
        outbox_columns = tuple(outbox.keys())
        with self.assertRaises(sqlite3.IntegrityError), connection:
            connection.execute(
                "INSERT INTO telegram_outbox ("
                + ",".join(outbox_columns)
                + ") VALUES ("
                + ",".join("?" for _ in outbox_columns)
                + ")",
                tuple(outbox[column] for column in outbox_columns),
            )

    def test_orphan_loss_item_is_not_explicit_loss_even_with_fk_disabled(self) -> None:
        """A partial/bypassed write cannot turn a missing document into declared loss."""
        connection = self.state._connection
        part = connection.execute(
            """SELECT file_sha256,file_size FROM telegram_outbox_parts
               WHERE outbox_id=? AND part_index=?""",
            (self.target.outbox_id, self.target.part_index),
        ).fetchone()
        assert part is not None
        binding = DeliveryReceipts(connection, StateError)._binding(
            "final", self.target.outbox_id, self.target.part_index
        )
        assert binding is not None
        connection.execute("DROP TRIGGER artifact_loss_items_insert_guard")
        # Preserve the historical trigger name while modelling a prior partial
        # schema.  `ledger_head()` must reject the orphan itself, not trust a
        # trigger's presence or SQLite foreign-key setting.
        connection.execute(
            """CREATE TRIGGER artifact_loss_items_insert_guard
               BEFORE INSERT ON artifact_loss_items WHEN 0
               BEGIN SELECT RAISE(ABORT, 'fixture'); END"""
        )
        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            with connection:
                connection.execute(
                    """INSERT INTO artifact_loss_items
                       (operation_id,outbox_id,part_index,source_binding_hash,file_sha256,file_size)
                       VALUES(?,?,?,?,?,?)""",
                    (
                        str(uuid.uuid4()),
                        self.target.outbox_id,
                        self.target.part_index,
                        binding,
                        part["file_sha256"],
                        part["file_size"],
                    ),
                )
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

        with self.assertRaisesRegex(StateError, "orphaned item"):
            ArtifactLossState(connection, StateError).ledger_head()
        inventory = inspect_artifact_recovery_inventory(
            self._snapshot("orphan-loss-item.db"), self.spool
        )
        self.assertFalse(inventory.ledger_valid)
        self.assertEqual(inventory.coverage, "incomplete")
        self.assertEqual(inventory.entries[0].availability, "unresolved_missing")

    def test_schema59_rejects_tampered_guard_definitions_and_injected_complete_chain(self) -> None:
        """Persisted no-op guards cannot make an injected record recovery evidence."""
        connection = self.state._connection
        part = connection.execute(
            """SELECT file_sha256,file_size FROM telegram_outbox_parts
               WHERE outbox_id=? AND part_index=?""",
            (self.target.outbox_id, self.target.part_index),
        ).fetchone()
        assert part is not None
        binding = DeliveryReceipts(connection, StateError)._binding(
            "final", self.target.outbox_id, self.target.part_index
        )
        assert binding is not None
        connection.execute("DROP TRIGGER artifact_loss_operations_append_only")
        connection.execute(
            """CREATE TRIGGER artifact_loss_operations_append_only
               BEFORE INSERT ON artifact_loss_operations WHEN 0
               BEGIN SELECT RAISE(ABORT, 'fixture'); END"""
        )
        connection.execute("DROP TRIGGER artifact_loss_items_insert_guard")
        connection.execute(
            """CREATE TRIGGER artifact_loss_items_insert_guard
               BEFORE INSERT ON artifact_loss_items WHEN 0
               BEGIN SELECT RAISE(ABORT, 'fixture'); END"""
        )
        operation_id = str(uuid.uuid4())
        audit = "a" * 64
        applied_at = "2026-11-19T00:00:00+00:00"
        items = [
            {
                "outbox_id": self.target.outbox_id,
                "part_index": self.target.part_index,
                "source_binding_hash": binding,
                "file_sha256": part["file_sha256"],
                "file_size": part["file_size"],
            }
        ]
        snapshot = _digest(
            {
                "version": 1,
                "previous_commit_hash": _GENESIS,
                "recovery_audit_sha256": audit,
                "targets": items,
            }
        )
        commit = _digest(
            {
                "version": 1,
                "operation_id": operation_id,
                "sequence": 1,
                "previous_commit_hash": _GENESIS,
                "snapshot_hash": snapshot,
                "recovery_audit_sha256": audit,
                "reason": _REASON,
                "authority": _AUTHORITY,
                "applied_at": applied_at,
            }
        )
        with connection:
            connection.execute(
                """INSERT INTO artifact_loss_operations
                   (operation_id,sequence,previous_commit_hash,snapshot_hash,recovery_audit_sha256,
                    reason,authority,applied_at,commit_hash)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    operation_id,
                    1,
                    _GENESIS,
                    snapshot,
                    audit,
                    _REASON,
                    _AUTHORITY,
                    applied_at,
                    commit,
                ),
            )
            connection.execute(
                """INSERT INTO artifact_loss_items
                   (operation_id,outbox_id,part_index,source_binding_hash,file_sha256,file_size)
                   VALUES(?,?,?,?,?,?)""",
                (
                    operation_id,
                    self.target.outbox_id,
                    self.target.part_index,
                    binding,
                    part["file_sha256"],
                    part["file_size"],
                ),
            )

        self.assertEqual(ArtifactLossState(connection, StateError).ledger_head(), (1, commit))
        inventory = inspect_artifact_recovery_inventory(
            self._snapshot("injected-complete-chain.db"), self.spool
        )

        self.assertFalse(inventory.ledger_valid)
        self.assertEqual(inventory.coverage, "incomplete")
        self.assertEqual(inventory.entries[0].availability, "unresolved_missing")

    def test_reappeared_file_and_invalid_ledger_never_skip_a_required_artifact(self) -> None:
        record = self._declare()
        target_path = self._target_path()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("fictional report\n", encoding="utf-8")
        restored = inspect_artifact_recovery_inventory(self._snapshot("restored.db"), self.spool)
        self.assertEqual(restored.coverage, "complete_artifact_coverage")
        self.assertEqual(restored.entries[0].availability, "present_required")

        target_path.unlink()
        with self.state._connection:
            self.state._connection.execute(
                """INSERT INTO artifact_loss_operations
                   (operation_id,sequence,previous_commit_hash,snapshot_hash,recovery_audit_sha256,
                    reason,authority,applied_at,commit_hash)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    str(uuid.uuid4()),
                    2,
                    record.commit_hash,
                    "b" * 64,
                    "c" * 64,
                    "irrecoverable_after_search",
                    "local_owner_cli",
                    "2026-11-19T00:00:00+00:00",
                    "d" * 64,
                ),
            )
        with self.assertRaises(StateError):
            self.state.declare_artifact_loss(
                (self.target,),
                spool_root=self.spool,
                recovery_audit_sha256="a" * 64,
                expected_snapshot=record.snapshot,
                operation_id=record.operation_id,
                acknowledge_irrecoverable_loss=True,
            )
        invalid = inspect_artifact_recovery_inventory(
            self._snapshot("invalid-ledger.db"), self.spool
        )
        self.assertFalse(invalid.ledger_valid)
        self.assertEqual(invalid.coverage, "incomplete")
        self.assertEqual(invalid.entries[0].availability, "unresolved_missing")

    def test_unknown_requires_an_exact_v1_receipt(self) -> None:
        timestamp = "2026-11-19T00:00:00+00:00"
        with self.state._immediate_transaction():
            receipts = DeliveryReceipts(self.state._connection, StateError)
            receipts.begin(
                "final",
                self.target.outbox_id,
                "fictional-v1-receipt",
                self.target.part_index,
                timestamp,
            )
            receipts.record(
                "final",
                self.target.outbox_id,
                "fictional-v1-receipt",
                self.target.part_index,
                801,
                timestamp,
            )
            self.state._connection.execute(
                """UPDATE telegram_outbox_parts SET telegram_message_id=?,delivered_at=?,
                   receipt_validation_version=1 WHERE outbox_id=? AND part_index=?""",
                (801, timestamp, self.target.outbox_id, self.target.part_index),
            )
        verified = inspect_artifact_recovery_inventory(self._snapshot("unknown-v1.db"), self.spool)
        self.assertEqual(verified.coverage, "complete_artifact_coverage")
        self.assertEqual(verified.entries[0].availability, "receipt_verified_unknown")
        with self.state._connection:
            self.state._connection.execute(
                """UPDATE telegram_outbox_parts SET receipt_validation_version=0
                   WHERE outbox_id=? AND part_index=?""",
                (self.target.outbox_id, self.target.part_index),
            )
        unverified = inspect_artifact_recovery_inventory(
            self._snapshot("unknown-unverified.db"), self.spool
        )
        self.assertEqual(unverified.coverage, "incomplete")
        self.assertEqual(unverified.entries[0].availability, "unresolved_missing")

    def test_private_batch_cli_is_idempotent_and_cannot_run_with_public_selection(self) -> None:
        selection = self.root / "loss-targets.json"
        selection.write_text(
            json.dumps(
                {
                    "version": 1,
                    "targets": [
                        {"outbox_id": self.target.outbox_id, "part_index": self.target.part_index}
                    ],
                }
            ),
            encoding="utf-8",
        )
        selection.chmod(0o600)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "artifact-loss",
                        "preview",
                        str(self.state_path),
                        "--spool-root",
                        str(self.spool),
                        "--targets",
                        str(selection),
                        "--recovery-audit-sha256",
                        "e" * 64,
                    ]
                ),
                0,
            )
        preview = json.loads(output.getvalue())
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "artifact-loss",
                        "apply",
                        str(self.state_path),
                        "--spool-root",
                        str(self.spool),
                        "--targets",
                        str(selection),
                        "--recovery-audit-sha256",
                        "e" * 64,
                        "--snapshot",
                        preview["snapshot"],
                        "--operation-id",
                        preview["operation_id"],
                        "--acknowledge-irrecoverable-loss",
                    ]
                ),
                0,
            )
        record = json.loads(output.getvalue())
        self.assertEqual(record["target_count"], 1)
        selection.chmod(0o644)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "artifact-loss",
                        "preview",
                        str(self.state_path),
                        "--spool-root",
                        str(self.spool),
                        "--targets",
                        str(selection),
                        "--recovery-audit-sha256",
                        "e" * 64,
                    ]
                ),
                2,
            )
        self.assertIn("mode-0600", output.getvalue())

    def test_private_batch_rejects_permission_change_during_read(self) -> None:
        """A target list must stay private throughout the file-descriptor read."""
        selection = self.root / "loss-targets-race.json"
        selection.write_text(
            json.dumps(
                {
                    "version": 1,
                    "targets": [
                        {"outbox_id": self.target.outbox_id, "part_index": self.target.part_index}
                    ],
                }
            ),
            encoding="utf-8",
        )
        selection.chmod(0o600)
        original_open = artifact_loss_cli.os.open

        def open_then_relax_permissions(path: Path, flags: int) -> int:
            descriptor = original_open(path, flags)
            selection.chmod(0o644)
            return descriptor

        try:
            with patch.object(
                artifact_loss_cli.os, "open", side_effect=open_then_relax_permissions
            ):
                with self.assertRaisesRegex(StateError, "changed during read"):
                    artifact_loss_cli._load_targets(selection)
        finally:
            selection.chmod(0o600)

    def test_two_owner_connections_cannot_declare_the_same_artifact_twice(self) -> None:
        first = self.state.preview_artifact_loss(
            (self.target,), spool_root=self.spool, recovery_audit_sha256="f" * 64
        )
        second = self.state.preview_artifact_loss(
            (self.target,), spool_root=self.spool, recovery_audit_sha256="f" * 64
        )
        barrier = threading.Barrier(2)
        records: list[object] = []
        errors: list[BaseException] = []

        def apply(preview: ArtifactLossPreview) -> None:
            try:
                with closing(HubState.open_existing(self.state_path)) as owner:
                    barrier.wait(timeout=5)
                    records.append(
                        owner.declare_artifact_loss(
                            (self.target,),
                            spool_root=self.spool,
                            recovery_audit_sha256="f" * 64,
                            expected_snapshot=preview.snapshot,
                            operation_id=preview.operation_id,
                            acknowledge_irrecoverable_loss=True,
                        )
                    )
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=apply, args=(preview,)) for preview in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(records), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StateError)

    def test_schema58_migration_is_atomic_and_additive(self) -> None:
        legacy = self.root / "fictional-schema58.db"
        project_historical_database(self.state_path, legacy, 58)
        with sqlite3.connect(legacy) as connection:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifact_loss_operations'"
                ).fetchone()
            )
        result = migrations.migrate_database(legacy, create_backup=False)
        self.assertEqual((result.previous_version, result.current_version), (58, 61))
        with sqlite3.connect(legacy) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM artifact_loss_operations").fetchone()[0], 0
            )

        broken = self.root / "fictional-schema58-broken.db"
        project_historical_database(self.state_path, broken, 58)
        before = (
            sqlite3.connect(broken)
            .execute("SELECT type,name,sql FROM sqlite_master ORDER BY name")
            .fetchall()
        )
        with patch.object(
            migrations, "MIGRATION_59", migrations.MIGRATION_59 + "\nINVALID FIXTURE SQL;"
        ):
            with self.assertRaises(sqlite3.Error):
                migrations.migrate_database(broken, create_backup=False)
        with sqlite3.connect(broken) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 58)
            self.assertEqual(
                connection.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY name"
                ).fetchall(),
                before,
            )

    def test_schema58_migration_refuses_missing_retirement_evidence(self) -> None:
        """Schema 59 must not bless a partial schema-58 predecessor."""
        broken = self.root / "fictional-schema58-missing-retirements.db"
        project_historical_database(self.state_path, broken, 58)
        with sqlite3.connect(broken) as connection:
            connection.execute("DROP TABLE telegram_artifact_retirements")
        before = (
            sqlite3.connect(broken)
            .execute("SELECT type,name,sql FROM sqlite_master ORDER BY name")
            .fetchall()
        )

        with self.assertRaisesRegex(RuntimeError, "retirement evidence is unavailable"):
            migrations.migrate_database(broken, create_backup=False)

        with sqlite3.connect(broken) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 58)
            self.assertEqual(
                connection.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY name"
                ).fetchall(),
                before,
            )

    def test_schema58_migration_refuses_missing_retirement_guard(self) -> None:
        """Schema 59 requires the immutable schema-58 predecessor, not just its table."""
        broken = self.root / "fictional-schema58-missing-retirement-guard.db"
        project_historical_database(self.state_path, broken, 58)
        with sqlite3.connect(broken) as connection:
            connection.execute("DROP TRIGGER telegram_artifact_retirements_no_delete")
        before = (
            sqlite3.connect(broken)
            .execute("SELECT type,name,sql FROM sqlite_master ORDER BY name")
            .fetchall()
        )

        with self.assertRaisesRegex(RuntimeError, "retirement evidence is unavailable"):
            migrations.migrate_database(broken, create_backup=False)

        with sqlite3.connect(broken) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 58)
            self.assertEqual(
                connection.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY name"
                ).fetchall(),
                before,
            )

    def test_pre_receipt_ledger_snapshot_is_not_a_compatible_inventory_input(self) -> None:
        legacy = self.root / "fictional-schema53.db"
        project_historical_database(self.state_path, legacy, 53)
        with self.assertRaisesRegex(ArtifactRecoveryInventoryError, "schema is unsupported"):
            inspect_artifact_recovery_inventory(legacy, self.spool)

    def test_schema58_snapshot_without_loss_ledger_keeps_missing_document_unresolved(self) -> None:
        """The schema-59 reader must retain its documented schema-58 fallback."""
        legacy = self.root / "fictional-schema58.db"
        project_historical_database(self.state_path, legacy, 58)

        inventory = inspect_artifact_recovery_inventory(legacy, self.spool)

        self.assertEqual(inventory.schema_version, 58)
        self.assertTrue(inventory.ledger_valid)
        self.assertIsNone(inventory.ledger_sequence)
        self.assertIsNone(inventory.ledger_head)
        self.assertEqual(inventory.coverage, "incomplete")
        self.assertEqual(inventory.entries[0].availability, "unresolved_missing")

    def test_schema58_hybrid_with_loss_tables_fails_closed(self) -> None:
        """A downgraded schema marker cannot bypass schema-59 ledger validation."""
        self._declare()
        hybrid = self._snapshot("fictional-schema58-hybrid.db")
        connection = sqlite3.connect(hybrid)
        try:
            connection.execute("PRAGMA user_version=58")
            connection.commit()
        finally:
            connection.close()

        inventory = inspect_artifact_recovery_inventory(hybrid, self.spool)

        self.assertEqual(inventory.schema_version, 58)
        self.assertFalse(inventory.ledger_valid)
        self.assertIsNone(inventory.ledger_sequence)
        self.assertIsNone(inventory.ledger_head)
        self.assertEqual(inventory.coverage, "incomplete")
        self.assertEqual(inventory.entries[0].availability, "unresolved_missing")

    def test_schema59_snapshot_without_loss_guard_fails_closed(self) -> None:
        """A partial ledger schema cannot certify an explicit-loss entry."""
        self._declare()
        damaged = self._snapshot("fictional-schema59-missing-loss-guard.db")
        connection = sqlite3.connect(damaged)
        try:
            connection.execute("DROP TRIGGER artifact_loss_operations_no_delete")
            connection.commit()
        finally:
            connection.close()

        inventory = inspect_artifact_recovery_inventory(damaged, self.spool)

        self.assertEqual(inventory.schema_version, 61)
        self.assertFalse(inventory.ledger_valid)
        self.assertEqual(inventory.coverage, "incomplete")
        self.assertEqual(inventory.entries[0].availability, "unresolved_missing")


if __name__ == "__main__":
    unittest.main()
