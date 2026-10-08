"""Preview-only delivery control prerequisite, using fictional state and no traffic."""

from __future__ import annotations

import io
import json
import sqlite3
import unittest
from contextlib import closing, redirect_stdout
from typing import Any
from unittest.mock import patch

from hermes_codex_router.cli import main
from hermes_codex_router.state import HubState, StateError
from tests import test_outbox_sender as fixtures


class DeliveryControlPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.TelegramOutboxSenderTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.job_id = self.fixture.ready_outbox("opencode", 151)
        self.state = HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.db = self.state._connection
        self.job = self.state.get_provider_job(self.job_id)
        self.outbox = self.state.get_telegram_outbox_for_job(self.job_id)
        with self.db:
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/project'")
            self.db.execute("UPDATE telegram_outbox SET status='unknown'")

    def preview(self, kind: str = "final_outbox", target: str | None = None):
        return self.state.preview_delivery_control(kind, target or self.outbox.outbox_id)

    def progress(self, name: str = "example-progress", item: str = "example-item") -> str:
        with self.db:
            self.db.execute(
                """INSERT OR IGNORE INTO provider_execution_checkpoints
                   (job_id,provider_thread_id,project_root,updated_at)
                   VALUES (?,'example-thread','/home/example/project','example-time')""",
                (self.job_id,),
            )
            sequence = self.db.execute(
                """INSERT INTO provider_visible_items(job_id,item_id,phase,visible_text,created_at)
                   VALUES (?,?,'commentary','Example progress','example-time')""",
                (self.job_id, item),
            ).lastrowid
            self.db.execute(
                """INSERT INTO provider_progress_deliveries
                   (progress_id,item_sequence,job_id,sender_agent_id,chat_id,thread_id,
                    telegram_html,status,available_at,created_at,updated_at)
                   VALUES (?,?,?,'opencode',?,?,'Example progress','unknown',
                           'example-time','example-time','example-time')""",
                (name, sequence, self.job_id, self.outbox.chat_id, self.outbox.thread_id),
            )
        return name

    def seed(self, kind: str = "final_outbox", target: str | None = None, **changes):
        target = target or self.outbox.outbox_id
        progress = self.db.execute(
            "SELECT item_sequence FROM provider_progress_deliveries WHERE progress_id=?", (target,)
        ).fetchone()
        result = self.db.execute(
            "SELECT result_id FROM provider_job_results WHERE job_id=?", (self.job_id,)
        ).fetchone()
        values = dict(
            disposition_id="example-decision-" + target,
            target_kind=kind,
            outbox_id=target if kind == "final_outbox" else None,
            progress_id=target if kind == "progress_delivery" else None,
            item_sequence=progress[0] if progress else None,
            job_id=self.job_id,
            result_id=result[0] if kind == "final_outbox" and result else None,
            topic_id=self.job.topic_id,
            topic_sequence=self.job.topic_sequence,
            project_id="example-project",
            session_id=self.job.session_id,
            session_generation=self.job.session_generation,
            sender_agent_id="opencode",
            chat_id=self.outbox.chat_id,
            thread_id=self.outbox.thread_id,
            execution_scope_at_consent="root:/home/example/project",
            delivery_status_at_consent="unknown",
            action="reconcile_delivery_control_wait",
            snapshot_version=1,
            snapshot="a" * 64,
            authority="local_owner_cli",
            applied_at="2026-10-08T00:00:00+00:00",
            terminal_evidence_job_id=None,
            resolution_job_id=None,
        )
        values.update(changes)
        with self.db:
            self.db.execute(
                f"INSERT INTO telegram_delivery_control_dispositions ({','.join(values)}) "
                f"VALUES ({','.join('?' for _ in values)})",
                tuple(values.values()),
            )

    def test_preview_is_read_only_bounded_and_has_no_control_effect(self) -> None:
        before = self.db.serialize()
        preview = self.preview()
        self.assertEqual(self.db.serialize(), before)
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(preview.capability, "preview_only")
        self.assertFalse(preview.apply_available)
        self.assertEqual(preview.control_effect, "not_enabled")
        self.assertRegex(preview.snapshot, r"^[0-9a-f]{64}$")
        self.assertIsNone(preview.disposition_snapshot)
        self.assertFalse(preview.binding_matches)
        with closing(HubState.open_read_only(self.fixture.config.state_path)) as reader:
            self.assertEqual(
                reader.preview_delivery_control("final_outbox", self.outbox.outbox_id), preview
            )
            self.assertFalse(reader._connection.in_transaction)

    def test_both_parked_kinds_and_exhausted_failed_are_eligible(self) -> None:
        progress = self.progress()
        for kind, table, key, target in (
            ("final_outbox", "telegram_outbox", "outbox_id", self.outbox.outbox_id),
            ("progress_delivery", "provider_progress_deliveries", "progress_id", progress),
        ):
            with self.subTest(kind=kind):
                self.preview(kind, target)
                with self.db:
                    self.db.execute(
                        f"UPDATE {table} SET status='failed',attempt_count=20 WHERE {key}=?",
                        (target,),
                    )
                self.assertEqual(self.preview(kind, target).delivery_status, "failed")
                with self.db:
                    self.db.execute(f"UPDATE {table} SET attempt_count=19 WHERE {key}=?", (target,))
                with self.assertRaises(StateError):
                    self.preview(kind, target)
                self.assertFalse(self.db.in_transaction)

    def test_rejects_unparked_progress_leases_and_noncommentary(self) -> None:
        progress = self.progress()
        for status in ("pending", "sending", "delivered", "superseded"):
            with self.subTest(status=status):
                with self.db:
                    self.db.execute(
                        "UPDATE provider_progress_deliveries SET status=? WHERE progress_id=?",
                        (status, progress),
                    )
                with self.assertRaises(StateError):
                    self.preview("progress_delivery", progress)
        with self.db:
            self.db.execute(
                "UPDATE provider_progress_deliveries SET status='unknown',lease_token='example-lease'"
            )
        with self.assertRaises(StateError):
            self.preview("progress_delivery", progress)
        with self.db:
            self.db.execute("UPDATE provider_progress_deliveries SET lease_token=NULL")
            self.db.execute("UPDATE provider_visible_items SET phase='final_answer'")
        with self.assertRaises(StateError):
            self.preview("progress_delivery", progress)

    def test_rejects_invalid_id_kind_binding_and_legacy_scope(self) -> None:
        for kind, target in (
            ("other", "example"),
            ("final_outbox", ""),
            ("final_outbox", "x" * 129),
            ("final_outbox", "missing"),
        ):
            with self.subTest(kind=kind, target=target), self.assertRaises(StateError):
                self.state.preview_delivery_control(kind, target)
        for scope in (
            None,
            "",
            "project:example-project",
            "root:relative",
            "root:/home/example/../project",
            "root://home/example/project",
        ):
            with self.subTest(scope=scope):
                with self.db:
                    self.db.execute("UPDATE topics SET execution_scope=?", (scope,))
                with self.assertRaises(StateError):
                    self.preview()
        with self.db:
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/project'")
            self.db.execute("UPDATE telegram_outbox SET thread_id=999")
        with self.assertRaises(StateError):
            self.preview()

    def test_snapshot_covers_all_parts_receipts_artifacts_and_progress_item(self) -> None:
        with self.db:
            for index in range(2, 80):
                self.db.execute(
                    "INSERT INTO telegram_outbox_parts(outbox_id,part_index,telegram_html) VALUES(?,?,'Example')",
                    (self.outbox.outbox_id, index),
                )
        token = self.preview().snapshot
        with self.db:
            self.db.execute(
                "UPDATE telegram_outbox_parts SET telegram_html='Changed' WHERE part_index=79"
            )
        self.assertNotEqual(self.preview().snapshot, token)
        token = self.preview().snapshot
        with self.db:
            self.db.execute(
                "UPDATE telegram_outbox_parts SET receipt_validation_version=1 WHERE part_index=1"
            )
        self.assertNotEqual(self.preview().snapshot, token)
        token = self.preview().snapshot
        with self.db:
            self.db.execute(
                """UPDATE telegram_outbox_parts SET part_type='document',file_path='/home/example/spool/file',file_name='example.txt',file_size=7,file_sha256=? WHERE part_index=79""",
                ("b" * 64,),
            )
        self.assertNotEqual(self.preview().snapshot, token)
        progress = self.progress()
        token = self.preview("progress_delivery", progress).snapshot
        with self.db:
            self.db.execute("UPDATE provider_visible_items SET visible_text='Changed progress'")
        self.assertNotEqual(self.preview("progress_delivery", progress).snapshot, token)

    def test_failed_indeterminate_requires_retained_independent_exact_proof(self) -> None:
        self.progress()
        with self.db:
            self.db.execute("DELETE FROM provider_job_results WHERE job_id=?", (self.job_id,))
            self.db.execute(
                "UPDATE provider_jobs SET status='indeterminate' WHERE job_id=?", (self.job_id,)
            )
            self.db.execute("UPDATE telegram_outbox SET status='failed',attempt_count=20")
            self.db.execute(
                "UPDATE provider_execution_checkpoints SET provider_turn_id='example-turn'"
            )
        with self.assertRaises(StateError):
            self.preview()
        with self.db:
            self.db.execute(
                """INSERT INTO provider_turn_terminal_evidence
                (job_id,terminal_status,provider_thread_id,provider_turn_id,project_root,observed_at)
                VALUES (?,'interrupted','wrong-thread','example-turn','/home/example/project','example-time')""",
                (self.job_id,),
            )
        with self.assertRaises(StateError):
            self.preview()
        with self.db:
            self.db.execute(
                "UPDATE provider_turn_terminal_evidence SET provider_thread_id='example-thread'"
            )
        for field, value in (
            ("provider_turn_id", "wrong-turn"),
            ("project_root", "/home/example/wrong-root"),
        ):
            with self.subTest(field=field):
                with self.db:
                    self.db.execute(
                        f"UPDATE provider_turn_terminal_evidence SET {field}=?", (value,)
                    )
                with self.assertRaises(StateError):
                    self.preview()
                with self.db:
                    self.db.execute(
                        f"UPDATE provider_turn_terminal_evidence SET {field}=(SELECT {field} FROM provider_execution_checkpoints WHERE job_id=?)",
                        (self.job_id,),
                    )
        # Historical native root remains exact evidence after a legal live scope
        # change; the stopped-completion proof needs no saved result/text.
        with self.db:
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/changed-scope'")
            self.db.execute(
                "UPDATE provider_turn_terminal_evidence SET terminal_status='completed'"
            )
        token = self.preview().snapshot
        self.seed(delivery_status_at_consent="failed", terminal_evidence_job_id=self.job_id)
        with self.assertRaises(sqlite3.IntegrityError), self.db:
            self.db.execute("DELETE FROM provider_turn_terminal_evidence")
        with self.db:
            self.db.execute("UPDATE provider_turn_terminal_evidence SET observed_at='changed-time'")
        self.assertNotEqual(self.preview().snapshot, token)

    def test_resolution_proof_and_unknown_notice_remain_distinct(self) -> None:
        with self.db:
            self.db.execute("DELETE FROM provider_job_results WHERE job_id=?", (self.job_id,))
            self.db.execute(
                "UPDATE provider_jobs SET status='indeterminate' WHERE job_id=?", (self.job_id,)
            )
        self.preview()  # Unknown notices remain compatible with evidence-only recovery.
        with self.db:
            self.db.execute("UPDATE telegram_outbox SET status='failed',attempt_count=20")
            self.db.execute(
                "INSERT INTO provider_job_resolutions VALUES (?,'acknowledged','example-time')",
                (self.job_id,),
            )
        self.preview()
        self.seed(delivery_status_at_consent="failed", resolution_job_id=self.job_id)
        with self.assertRaises(sqlite3.IntegrityError), self.db:
            self.db.execute("DELETE FROM provider_job_resolutions")

    def test_historical_binding_survives_session_scope_changes_and_late_progress_result(
        self,
    ) -> None:
        progress = self.progress()
        self.seed()
        self.seed("progress_delivery", progress)
        with self.db:
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/new-root'")
            self.db.execute(
                "UPDATE agent_sessions SET status='archived',model='changed-model',effort='low'"
            )
        for kind, target in (
            ("final_outbox", self.outbox.outbox_id),
            ("progress_delivery", progress),
        ):
            preview = self.preview(kind, target)
            self.assertTrue(preview.binding_matches)
            self.assertEqual(preview.disposition_snapshot, "a" * 64)
            self.assertEqual(preview.control_effect, "not_enabled")
        other = self.progress("example-other-progress", "example-other-item")
        self.assertFalse(self.preview("progress_delivery", other).binding_matches)

    def test_ledger_null_safe_branches_bounds_foreign_keys_and_immutability(self) -> None:
        progress = self.progress()
        bad: tuple[dict[str, Any], ...] = (
            {"disposition_id": None},
            {"outbox_id": None},
            {"target_kind": None},
            {"target_kind": "other"},
            {"progress_id": progress},
            {"item_sequence": 1},
            {"job_id": "missing"},
            {"snapshot": "A" * 64},
            {"snapshot": None},
            {"project_id": ""},
            {"execution_scope_at_consent": "project:example"},
            {"delivery_status_at_consent": "delivered"},
            {"authority": "telegram"},
            {"resolution_job_id": "missing"},
            {"session_generation": 0},
        )
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(sqlite3.IntegrityError):
                self.seed(**changes)
        result = self.db.execute("SELECT result_id FROM provider_job_results").fetchone()[0]
        progress_bad: tuple[dict[str, Any], ...] = (
            {"item_sequence": None},
            {"outbox_id": self.outbox.outbox_id},
            {"result_id": result},
            {"progress_id": None},
        )
        for changes in progress_bad:
            with self.subTest(changes=changes), self.assertRaises(sqlite3.IntegrityError):
                self.seed("progress_delivery", progress, **changes)
        self.seed()
        self.seed("progress_delivery", progress)
        with self.assertRaises(sqlite3.IntegrityError):
            self.seed(disposition_id="example-duplicate")
        for sql in (
            "UPDATE telegram_delivery_control_dispositions SET snapshot=snapshot",
            "DELETE FROM telegram_delivery_control_dispositions",
            "DELETE FROM telegram_outbox",
            "DELETE FROM provider_progress_deliveries",
            "DELETE FROM provider_visible_items",
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError), self.db:
                self.db.execute(sql)

    def test_coherent_read_snapshot_when_other_connection_commits_mid_preview(self) -> None:
        original = self.preview().snapshot
        with closing(sqlite3.connect(self.fixture.config.state_path)) as other:
            changed = False

            def concurrent_change(sql: str) -> None:
                nonlocal changed
                if not changed and "FROM telegram_outbox_parts" in sql:
                    changed = True
                    with other:
                        other.execute(
                            "UPDATE telegram_outbox_parts SET telegram_html='Changed during preview'"
                        )

            self.db.set_trace_callback(concurrent_change)
            try:
                self.assertEqual(self.preview().snapshot, original)
            finally:
                self.db.set_trace_callback(None)
            self.assertTrue(changed)
        self.assertNotEqual(self.preview().snapshot, original)

    def test_seeded_disposition_does_not_unlock_session_or_fifo_or_old_lifetime_binding(
        self,
    ) -> None:
        self.seed()
        tail, created = self.state.enqueue_provider_job(
            idempotency_key="example-control-preview-tail",
            chat_id=self.job.chat_id,
            message_id=152,
            topic_id=self.job.topic_id,
            agent_id=self.job.agent_id,
            session_id=self.job.session_id,
            session_generation=self.job.session_generation,
            provider_session_id=self.job.provider_session_id,
            model=self.job.model,
            effort=self.job.effort,
            payload_text="Example queued tail",
            context_watermark=None,
            handoff_id=None,
        )
        self.assertTrue(created)
        self.assertEqual(self.state.get_provider_job(tail.job_id).status, "queued")
        self.assertIsNone(self.state.lease_provider_job("opencode", "example-worker"))
        with self.assertRaises(StateError):
            self.state.new_active_session(
                self.job.topic_id, expected_session_id=self.job.session_id
            )
        old = self.state.preview_delivery_hold(self.outbox.outbox_id)
        self.assertEqual(old.hold_status, "outstanding")
        self.state.release_delivery_hold(
            self.outbox.outbox_id,
            expected_snapshot=old.snapshot,
            continue_without_confirmed_delivery=True,
        )
        with self.assertRaises(sqlite3.IntegrityError), self.db:
            self.db.execute("UPDATE topics SET execution_scope='root:/home/example/new-root'")
        with self.assertRaises(StateError):
            self.state.set_writer_mode(self.job.session_id, "local")

    def test_progress_binding_ignores_a_result_saved_after_the_decision(self) -> None:
        progress = self.progress()
        with self.db:
            saved = dict(self.db.execute("SELECT * FROM provider_job_results").fetchone())
            self.db.execute("DELETE FROM provider_job_results")
            self.db.execute(
                "UPDATE provider_jobs SET status='executing',lease_owner='example-worker',lease_token='example-lease',lease_expires_at='example-time'"
            )
        self.seed("progress_delivery", progress)
        self.assertTrue(self.preview("progress_delivery", progress).binding_matches)
        with self.db:
            self.db.execute(
                f"INSERT INTO provider_job_results ({','.join(saved)}) VALUES ({','.join('?' for _ in saved)})",
                tuple(saved.values()),
            )
            self.db.execute(
                "UPDATE provider_jobs SET status='result_ready',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL"
            )
        self.assertTrue(self.preview("progress_delivery", progress).binding_matches)

    def test_cli_is_preview_only_and_reads_no_configuration_or_content(self) -> None:
        with patch(
            "hermes_codex_router.cli.load_hub_config", side_effect=AssertionError("no config")
        ) as config:
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    main(
                        [
                            "delivery-control",
                            str(self.fixture.config.state_path),
                            "final_outbox",
                            self.outbox.outbox_id,
                        ]
                    ),
                    0,
                )
            result = json.loads(output.getvalue())
            self.assertFalse(result["apply_available"])
            self.assertEqual(result["control_effect"], "not_enabled")
            for value in ("durable task", "opencode result", "/home/example/"):
                self.assertNotIn(value, output.getvalue())
            config.assert_not_called()
        with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()):
            main(
                [
                    "delivery-control",
                    str(self.fixture.config.state_path),
                    "final_outbox",
                    self.outbox.outbox_id,
                    "--apply",
                ]
            )

    def test_missing_or_old_state_refused_without_creation_or_migration(self) -> None:
        missing = self.fixture.base / "missing-control.db"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(["delivery-control", str(missing), "final_outbox", self.outbox.outbox_id]), 2
            )
        self.assertFalse(missing.exists())
        with self.db:
            self.db.execute("PRAGMA user_version=45")
        before = self.db.serialize()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(
                main(
                    [
                        "delivery-control",
                        str(self.fixture.config.state_path),
                        "final_outbox",
                        self.outbox.outbox_id,
                    ]
                ),
                2,
            )
        self.assertEqual(self.db.serialize(), before)

    def test_nested_preview_does_not_take_ownership_of_caller_transaction(self) -> None:
        with self.state._immediate_transaction():
            with self.assertRaises(StateError):
                self.preview()
            self.assertTrue(self.db.in_transaction)
