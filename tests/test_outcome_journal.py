"""Passive outcome evidence must not invent acceptance, usage or execution time."""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from hermes_codex_router.cli import main
from hermes_codex_router.state import HubState, StateError


class OutcomeJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="example-outcomes-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.db"
        self.state = HubState.open(self.path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.topic = self.state.observe_topic(
            project_id="example-project", chat_id=-1001234567890, thread_id=77, title="Example"
        )
        self.session = self.state.activate_agent(
            self.topic.topic_id, "codex", "example-model", "high"
        )
        self.poison = "fictional-protected-content"

    def enqueue(self, number: int = 1):
        job, _ = self.state.enqueue_provider_job(
            idempotency_key=f"example:{number}",
            chat_id=self.topic.chat_id,
            message_id=number,
            topic_id=self.topic.topic_id,
            agent_id=self.session.agent_id,
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model=self.session.model,
            effort=self.session.effort,
            payload_text=self.poison,
        )
        return job

    def complete(self, *, model: str | None = "example-model"):
        job = self.enqueue()
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, lease.lease_token)
        result = self.state.commit_provider_result(
            job.job_id,
            lease.lease_token,
            visible_response=self.poison,
            sender_agent_id="codex",
            telegram_html=self.poison,
            actual_model=model,
            safe_metadata_json=json.dumps({"usage": 999, "private": self.poison}),
        )
        assert result is not None
        return job, result

    def read(self, job_id: str):
        state = self.state.open_read_only(self.path)
        try:
            return state.provider_job_outcome(job_id).as_dict()
        finally:
            state.close()

    def test_committed_result_does_not_establish_acceptance_or_observed_model_usage(self) -> None:
        job, result = self.complete(model="example-reported-label")
        outcome = self.read(job.job_id)
        self.assertEqual(outcome["result"]["result_id"], result.result_id)
        self.assertEqual(outcome["acceptance"]["decision"], "unknown")
        self.assertEqual(outcome["participant"]["requested_model"], "example-model")
        self.assertEqual(outcome["participant"]["stored_model_label"], "example-reported-label")
        self.assertIsNone(outcome["participant"]["observed_model"])
        self.assertIsNone(outcome["participant"]["observed_effort"])
        self.assertIsNone(outcome["participant"]["runtime"])
        self.assertIsNone(outcome["usage"]["tokens"])
        self.assertIsNone(outcome["usage"]["monetary_cost"])
        self.assertTrue(outcome["diagnostic_only"])
        self.assertFalse(outcome["productive_replay_authorized"])
        self.assertNotIn(self.poison, json.dumps(outcome))

    def test_current_settings_and_heartbeat_do_not_rewrite_snapshot(self) -> None:
        job, _ = self.complete()
        before = self.read(job.job_id)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE agent_sessions SET model='example-new-model', effort='low' WHERE session_id=?",
                (job.session_id,),
            )
            self.state._connection.execute(
                "UPDATE provider_jobs SET updated_at='2099-01-01T00:00:00+00:00' WHERE job_id=?",
                (job.job_id,),
            )
        self.assertEqual(self.read(job.job_id), before)

    def test_delivered_failure_notice_is_not_a_result_delivery(self) -> None:
        job = self.enqueue()
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.terminate_provider_job_with_notice(
            job.job_id,
            lease.lease_token,
            expected_status="leased",
            status="failed",
            error_class="pre_execution",
            error_code="example-preparation",
            error_detail=self.poison,
            sender_agent_id="codex",
            telegram_html=self.poison,
        )
        outbox = self.state.lease_telegram_outbox("codex", "example-sender")
        assert outbox is not None and outbox.lease_token is not None
        self.state.mark_telegram_outbox_delivered(
            outbox.outbox_id, outbox.lease_token, telegram_message_id=100
        )
        outcome = self.read(job.job_id)
        self.assertIsNone(outcome["result"])
        self.assertIsNone(outcome["result_delivery"])
        self.assertEqual(outcome["acceptance"]["decision"], "unknown")
        self.assertNotIn(self.poison, json.dumps(outcome))

    def test_elapsed_intervals_keep_source_semantics_and_unknown_native_duration(self) -> None:
        job, _ = self.complete()
        start = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET created_at=?, provider_started_at=? WHERE job_id=?",
                (start.isoformat(), (start + timedelta(seconds=7)).isoformat(), job.job_id),
            )
            self.state._connection.execute(
                "UPDATE provider_job_results SET created_at=? WHERE job_id=?",
                ((start + timedelta(seconds=27)).isoformat(), job.job_id),
            )
        timing = self.read(job.job_id)["timing"]
        self.assertEqual(timing["admission_to_result_commit"]["seconds"], 27)
        self.assertEqual(timing["latest_worker_phase_to_result_commit"]["seconds"], 20)
        self.assertEqual(
            timing["latest_worker_phase_to_result_commit"]["start_source"],
            "provider_jobs.provider_started_at",
        )
        self.assertIsNone(timing["native_execution_seconds"])
        self.assertIsNone(timing["queue_wait_seconds"])
        self.assertIsNone(timing["approval_wait_seconds"])
        for bad in (None, "invalid", "2026-01-01T00:00:00", "2099-01-01T00:00:00+00:00"):
            with self.subTest(start=bad), self.state._connection:
                self.state._connection.execute(
                    "UPDATE provider_jobs SET provider_started_at=? WHERE job_id=?",
                    (bad, job.job_id),
                )
            self.assertIsNone(
                self.read(job.job_id)["timing"]["latest_worker_phase_to_result_commit"]["seconds"]
            )

    def test_saved_empty_completion_and_native_terminal_evidence_are_separate(self) -> None:
        job = self.enqueue()
        with self.state._connection:
            self.state._connection.execute(
                "INSERT INTO provider_execution_checkpoints(job_id, provider_thread_id, project_root, completed_text, updated_at) VALUES (?, ?, ?, '', ?)",
                (job.job_id, self.poison, self.poison, "2026-01-01T00:00:00+00:00"),
            )
            self.state._connection.execute(
                "INSERT INTO provider_turn_terminal_evidence VALUES (?, 'failed', ?, ?, ?, ?)",
                (job.job_id, self.poison, self.poison, self.poison, "2026-01-01T00:00:01+00:00"),
            )
        outcome = self.read(job.job_id)
        self.assertTrue(outcome["execution"]["completion_saved"])
        self.assertFalse(outcome["execution"]["native_turn_id_saved"])
        self.assertEqual(outcome["execution"]["terminal_status"], "failed")
        self.assertEqual(outcome["acceptance"]["decision"], "unknown")
        self.assertNotIn(self.poison, json.dumps(outcome))

    def test_lineage_is_direct_bounded_and_does_not_copy_parent_result(self) -> None:
        parent, result = self.complete()
        children = [self.enqueue(number) for number in range(2, 72)]
        with self.state._connection:
            self.state._connection.executemany(
                "INSERT INTO provider_job_absorptions VALUES (?, ?, 'example-turn', ?)",
                [(child.job_id, parent.job_id, child.created_at) for child in children],
            )
        child = self.read(children[0].job_id)
        self.assertIsNone(child["result"])
        self.assertEqual(
            child["lineage"]["items"], [{"kind": "absorbed_into", "job_id": parent.job_id}]
        )
        self.assertNotIn(result.result_id, json.dumps(child))
        page = self.read(parent.job_id)["lineage"]
        self.assertEqual(page["total"], 70)
        self.assertTrue(page["truncated"])
        self.assertEqual(len(page["items"]), 64)

    def test_reads_are_query_only_and_leave_all_durable_state_unchanged(self) -> None:
        job, _ = self.complete()
        before = "\n".join(self.state._connection.iterdump())
        mode = self.path.stat().st_mode
        self.assertEqual(self.read(job.job_id), self.read(job.job_id))
        self.assertEqual("\n".join(self.state._connection.iterdump()), before)
        self.assertEqual(self.path.stat().st_mode, mode)
        with self.assertRaisesRegex(StateError, "outcome_job_not_found"):
            self.read("example-missing")
        for bad in ("", "x" * 129, "example\njob"):
            with self.assertRaisesRegex(StateError, "outcome_job_id_invalid"):
                self.read(bad)

    def test_readonly_open_never_creates_or_migrates_state(self) -> None:
        missing = Path(self.temp.name) / "missing.db"
        with self.assertRaises(StateError):
            HubState.open_read_only(missing)
        self.assertFalse(missing.exists())
        old = Path(self.temp.name) / "old.db"
        with sqlite3.connect(old) as connection:
            connection.execute("PRAGMA user_version=1")
        original = old.read_bytes()
        with self.assertRaises(StateError):
            HubState.open_read_only(old)
        self.assertEqual(old.read_bytes(), original)

    def test_artifacts_are_bounded_references_without_opening_or_disclosing_files(self) -> None:
        job, _ = self.complete()
        outbox = self.state.get_telegram_outbox_for_job(job.job_id)
        with self.state._connection:
            self.state._connection.executemany(
                """INSERT INTO telegram_outbox_parts(outbox_id, part_index, telegram_html,
                   part_type, file_path, file_name, file_size, file_sha256)
                   VALUES (?, ?, 'example-artifact', 'document', ?, ?, 9, ?)""",
                [(outbox.outbox_id, i, self.poison, self.poison, "a" * 64) for i in range(2, 72)],
            )
        # Existing state is opened before blocking every Python-level file read.
        reader = HubState.open_read_only(self.path)
        try:
            with (
                patch("builtins.open", side_effect=AssertionError("unexpected file read")),
                patch.object(Path, "open", side_effect=AssertionError("unexpected file read")),
            ):
                projection = reader.provider_job_outcome(job.job_id).as_dict()
        finally:
            reader.close()
        page = projection["artifacts"]
        self.assertEqual(page["total"], 70)
        self.assertEqual(len(page["items"]), 64)
        self.assertTrue(page["truncated"])
        self.assertEqual(page["items"][0]["size"], 9)
        self.assertEqual(page["items"][0]["sha256"], "a" * 64)
        self.assertIs(page["items"][0]["receipt_present"], False)
        self.assertNotIn(self.poison, json.dumps(projection))
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE telegram_outbox_parts SET telegram_message_id=999 WHERE outbox_id=? AND part_index=2",
                (outbox.outbox_id,),
            )
        self.assertIs(self.read(job.job_id)["artifacts"]["items"][0]["receipt_present"], True)

    def test_successful_delivery_and_incomplete_receipts_keep_acceptance_unknown(self) -> None:
        job, _ = self.complete()
        outbox = self.state.lease_telegram_outbox("codex", "example-sender")
        assert outbox is not None and outbox.lease_token is not None
        self.state.mark_telegram_outbox_delivered(
            outbox.outbox_id, outbox.lease_token, telegram_message_id=100
        )
        complete = self.read(job.job_id)
        self.assertTrue(complete["result_delivery"]["receipts_complete"])
        self.assertIsNotNone(complete["timing"]["result_commit_to_delivery_receipt"]["seconds"])
        self.assertEqual(complete["acceptance"]["decision"], "unknown")
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE telegram_outbox_parts SET telegram_message_id=NULL WHERE outbox_id=?",
                (outbox.outbox_id,),
            )
        incomplete = self.read(job.job_id)
        self.assertFalse(incomplete["result_delivery"]["receipts_complete"])
        self.assertIsNone(incomplete["timing"]["result_commit_to_delivery_receipt"]["end"])

    def test_sender_mismatch_refuses_result_delivery_ownership(self) -> None:
        job, _ = self.complete()
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE telegram_outbox SET sender_agent_id='example-other' WHERE job_id=?",
                (job.job_id,),
            )
        projection = self.read(job.job_id)
        self.assertIsNone(projection["result_delivery"])
        self.assertEqual(projection["artifacts"]["items"], [])
        self.assertEqual(projection["inconsistencies"], ["result_delivery_missing_or_mismatched"])

    def test_destination_mismatch_refuses_result_delivery_ownership(self) -> None:
        job, _ = self.complete()
        for column in ("chat_id", "thread_id"):
            with self.subTest(column=column):
                with self.state._connection:
                    self.state._connection.execute(
                        f"UPDATE telegram_outbox SET {column}={column}+1 WHERE job_id=?",
                        (job.job_id,),
                    )
                try:
                    projection = self.read(job.job_id)
                    self.assertIsNone(projection["result_delivery"])
                    self.assertEqual(projection["artifacts"]["items"], [])
                    self.assertEqual(
                        projection["inconsistencies"], ["result_delivery_missing_or_mismatched"]
                    )
                finally:
                    with self.state._connection:
                        self.state._connection.execute(
                            f"UPDATE telegram_outbox SET {column}={column}-1 WHERE job_id=?",
                            (job.job_id,),
                        )

    def test_cli_escapes_provider_control_characters_in_local_json(self) -> None:
        from types import SimpleNamespace

        label = "example\u009b31m\u202emodel"
        job, _ = self.complete(model=label)
        output = io.StringIO()
        with (
            patch(
                "hermes_codex_router.outcome_cli.load_external_worker_config",
                return_value=SimpleNamespace(state_path=self.path),
            ),
            redirect_stdout(output),
        ):
            code = main(["outcome-journal", "example-config.json", job.job_id])
        self.assertEqual(code, 0)
        self.assertNotIn("\u009b", output.getvalue())
        self.assertNotIn("\u202e", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["participant"]["stored_model_label"], label)

    def test_cli_sanitizes_malformed_config_structure_errors(self) -> None:
        for kind in (TypeError, KeyError, AttributeError, RecursionError):
            with self.subTest(kind=kind):
                output = io.StringIO()
                with (
                    patch(
                        "hermes_codex_router.outcome_cli.load_external_worker_config",
                        side_effect=kind(self.poison),
                    ),
                    redirect_stdout(output),
                ):
                    code = main(["outcome-journal", "example-config.json", "example-job"])
                self.assertEqual(code, 2)
                self.assertEqual(
                    json.loads(output.getvalue()),
                    {"ok": False, "error": "outcome_config_unavailable"},
                )

    def test_sqlite_json_unavailable_is_a_fixed_error_without_writable_fallback(self) -> None:
        job, _ = self.complete()
        reader = HubState.open_read_only(self.path)
        try:
            reader._connection.set_authorizer(
                lambda action, arg1, arg2, db, trigger: (
                    sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_FUNCTION and arg2 == "json_group_array"
                    else sqlite3.SQLITE_OK
                )
            )
            with self.assertRaisesRegex(StateError, "^outcome_projection_unavailable$"):
                reader.provider_job_outcome(job.job_id)
            self.assertEqual(reader._connection.execute("PRAGMA query_only").fetchone()[0], 1)
        finally:
            reader.close()

    def test_single_read_snapshot_survives_atomic_concurrent_result_delivery_change(self) -> None:
        job, _ = self.complete(model="example-before")
        reader = HubState.open_read_only(self.path)
        changed = False

        def concurrent_commit() -> int:
            nonlocal changed
            if not changed:
                changed = True
                with self.state._connection:
                    self.state._connection.execute(
                        "UPDATE provider_job_results SET actual_model='example-after' WHERE job_id=?",
                        (job.job_id,),
                    )
                    self.state._connection.execute(
                        "UPDATE telegram_outbox SET status='failed' WHERE job_id=?", (job.job_id,)
                    )
            return 0

        try:
            reader._connection.set_progress_handler(concurrent_commit, 30)
            projection = reader.provider_job_outcome(job.job_id).as_dict()
        finally:
            reader.close()
        self.assertTrue(changed)
        self.assertIn(
            (
                projection["participant"]["stored_model_label"],
                projection["result_delivery"]["status"],
            ),
            {("example-before", "pending"), ("example-after", "failed")},
        )

    def test_cli_close_and_projection_errors_emit_one_sanitized_json(self) -> None:
        from types import SimpleNamespace

        for failure in ("close", "projection", "serialize"):
            with self.subTest(failure=failure):
                state = Mock()
                state.provider_job_outcome.return_value.as_dict.return_value = {"example": "ok"}
                if failure == "close":
                    state.close.side_effect = OSError(self.poison)
                elif failure == "projection":
                    state.provider_job_outcome.side_effect = sqlite3.OperationalError(self.poison)
                else:
                    state.provider_job_outcome.return_value.as_dict.return_value = {
                        "invalid": b"fictional-protected-content"
                    }
                output = io.StringIO()
                with (
                    patch(
                        "hermes_codex_router.outcome_cli.load_external_worker_config",
                        return_value=SimpleNamespace(state_path=self.path),
                    ),
                    patch(
                        "hermes_codex_router.outcome_cli.HubState.open_read_only",
                        return_value=state,
                    ),
                    redirect_stdout(output),
                ):
                    code = main(["outcome-journal", "example-config.json", "example-job"])
                self.assertEqual(code, 2)
                self.assertEqual(
                    json.loads(output.getvalue()),
                    {"ok": False, "error": "outcome_projection_unavailable"},
                )
                state.close.assert_called_once()

    def test_cli_real_passive_loader_never_reads_credentials_or_invokes_provider(self) -> None:
        job, _ = self.complete()
        config = Path(self.temp.name) / "config.json"
        registry = Path(self.temp.name) / "projects.json"
        registry.write_text("{}")
        config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "owner_user_ids": [42],
                    "registry_path": str(registry),
                    "state_path": str(self.path),
                    "projects": [
                        {"project_id": "example-project", "telegram_chat_id": -1001234567890}
                    ],
                    "agents": [
                        {
                            "agent_id": "codex",
                            "display_name": "Example",
                            "telegram_username": "example_codex_bot",
                            "runtime": "codex",
                            "token_file": str(Path(self.temp.name) / "nonexistent-token"),
                            "terminal_enabled": True,
                            "default_model": "example-model",
                            "default_effort": "high",
                        }
                    ],
                }
            )
        )
        output = io.StringIO()
        with (
            patch(
                "hermes_codex_router.hub_config.read_telegram_token",
                side_effect=AssertionError("credential read"),
            ),
            patch("subprocess.run", side_effect=AssertionError("subprocess invocation")),
            patch("socket.socket", side_effect=AssertionError("network invocation")),
            redirect_stdout(output),
        ):
            code = main(["outcome-journal", str(config), job.job_id])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["task"]["job_id"], job.job_id)

    def test_cli_uses_passive_loader_and_sanitizes_config_errors(self) -> None:
        job, _ = self.complete()
        from types import SimpleNamespace

        output = io.StringIO()
        with (
            patch(
                "hermes_codex_router.outcome_cli.load_external_worker_config",
                return_value=SimpleNamespace(state_path=self.path),
            ) as loader,
            redirect_stdout(output),
        ):
            code = main(["outcome-journal", "example-config.json", job.job_id])
        self.assertEqual(code, 0)
        loader.assert_called_once_with(Path("example-config.json"))
        self.assertEqual(json.loads(output.getvalue())["task"]["job_id"], job.job_id)
        from hermes_codex_router.hub_config import HubConfigError

        output = io.StringIO()
        with (
            patch(
                "hermes_codex_router.outcome_cli.load_external_worker_config",
                side_effect=HubConfigError(self.poison),
            ),
            redirect_stdout(output),
        ):
            code = main(["outcome-journal", "example-config.json", job.job_id])
        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(output.getvalue()), {"ok": False, "error": "outcome_config_unavailable"}
        )
