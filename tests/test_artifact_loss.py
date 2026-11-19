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
from hermes_codex_router.artifact_loss_state import ArtifactLossPreview, ArtifactLossTarget
from hermes_codex_router.artifact_recovery_inventory import (
    ArtifactRecoveryInventoryError,
    inspect_artifact_recovery_inventory,
)
from hermes_codex_router.artifacts import artifact_spool_root, spool_staged_artifacts
from hermes_codex_router.cli import main
from hermes_codex_router.database_backup import backup_database
from hermes_codex_router.delivery_receipts import DeliveryReceipts
from hermes_codex_router.state import HubState, StateError
from tests.schema_fixtures import project_historical_database


class ArtifactLossTests(unittest.TestCase):
    def setUp(self) -> None:
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
        with patch(
            "hermes_codex_router.artifact_capabilities.EXPLICIT_ARTIFACT_LOSS_DECLARATION",
            False,
        ):
            with self.assertRaisesRegex(StateError, "does not permit artifact-loss declarations"):
                self.state.preview_artifact_loss(
                    (self.target,), spool_root=self.spool, recovery_audit_sha256="a" * 64
                )

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
        self.assertEqual((result.previous_version, result.current_version), (58, 59))
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

        self.assertEqual(inventory.schema_version, 59)
        self.assertFalse(inventory.ledger_valid)
        self.assertEqual(inventory.coverage, "incomplete")
        self.assertEqual(inventory.entries[0].availability, "unresolved_missing")


if __name__ == "__main__":
    unittest.main()
