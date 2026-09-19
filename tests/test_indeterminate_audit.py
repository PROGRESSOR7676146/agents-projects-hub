from __future__ import annotations

import io
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import test_codex_worker as fixtures

from hermes_codex_router.cli import main
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.indeterminate_audit import (
    classify_indeterminate_jobs,
    write_private_indeterminate_report,
)
from hermes_codex_router.state import HubState, StateError


class IndeterminateAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexQueueWorkerTests()
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _indeterminate(self, message_id: int, phase: str, *, notice: bool) -> str:
        state = HubState.open(self.fixture.config.state_path)
        try:
            topic = state.observe_topic(
                project_id=f"fictional-audit-{message_id}",
                chat_id=-1001234567890,
                thread_id=70 + message_id,
                title=f"Fictional audit {message_id}",
            )
            session = state.activate_agent(topic.topic_id, "codex", "example-model", "high")
            job, _ = state.enqueue_provider_job(
                idempotency_key=f"fictional-audit:{message_id}",
                chat_id=topic.chat_id,
                message_id=message_id,
                topic_id=topic.topic_id,
                agent_id="codex",
                session_id=session.session_id,
                session_generation=session.generation,
                model=session.model,
                effort=session.effort,
                payload_text=f"private-{phase}",
            )
            job_id = job.job_id
            lease = state.lease_provider_job("codex", f"worker-{message_id}")
            assert lease is not None and lease.lease_token is not None
            state.mark_provider_job_executing(job_id, lease.lease_token)
            journal = ExecutionJournal(state)
            if phase != "none":
                journal.record_thread(
                    job_id,
                    lease.lease_token,
                    f"thread-{message_id}",
                    self.fixture.registry.projects[0].root,
                )
            if phase in {"accepted", "partial", "completed"}:
                journal.record_turn(job_id, lease.lease_token, f"turn-{message_id}")
            if phase == "partial":
                journal.record_item(
                    job_id, lease.lease_token, f"item-{message_id}", "visible", "commentary"
                )
            if phase == "completed":
                journal.record_completion(job_id, lease.lease_token, "saved final")
            if notice:
                state.terminate_provider_job_with_notice(
                    job_id,
                    lease.lease_token,
                    status="indeterminate",
                    error_class="ambiguous_execution",
                    error_code="test_failure",
                    sender_agent_id="codex",
                    telegram_html="Outcome unknown",
                )
            else:
                state.mark_provider_job_indeterminate(
                    job_id, lease.lease_token, error_code="test_failure"
                )
            return job_id
        finally:
            state.close()

    def test_classifies_evidence_and_notice_without_exposing_content_or_writing_state(self) -> None:
        expected = {
            self._indeterminate(1, "completed", notice=True): "completed_checkpoint",
            self._indeterminate(2, "partial", notice=True): "partial_checkpoint",
            self._indeterminate(3, "accepted", notice=False): "accepted_without_visible_result",
            self._indeterminate(4, "thread", notice=False): "thread_without_accepted_turn",
            self._indeterminate(5, "none", notice=False): "no_execution_checkpoint",
        }

        report = classify_indeterminate_jobs(self.fixture.config.state_path)

        self.assertEqual(report["total"], 5)
        self.assertEqual({item["job_id"]: item["evidence"] for item in report["records"]}, expected)
        self.assertEqual(report["notice_status"], {"missing": 3, "pending": 2})
        rendered = json.dumps(report)
        self.assertNotIn("private-", rendered)
        self.assertNotIn("saved final", rendered)
        state = HubState.open(self.fixture.config.state_path)
        try:
            self.assertTrue(
                all(state.get_provider_job(job_id).status == "indeterminate" for job_id in expected)
            )
        finally:
            state.close()

    def test_private_report_is_exclusive_and_mode_0600(self) -> None:
        self._indeterminate(1, "none", notice=False)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "audit.json"
            report = classify_indeterminate_jobs(self.fixture.config.state_path)

            write_private_indeterminate_report(destination, report)

            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(json.loads(destination.read_text())["total"], 1)
            with self.assertRaises(FileExistsError):
                write_private_indeterminate_report(destination, report)

    def test_cli_prints_only_aggregate_and_writes_private_details(self) -> None:
        job_id = self._indeterminate(1, "none", notice=False)
        report = classify_indeterminate_jobs(self.fixture.config.state_path)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "audit.json"
            output = io.StringIO()
            with (
                patch(
                    "hermes_codex_router.cli.load_external_worker_config",
                    return_value=self.fixture.config,
                ),
                redirect_stdout(output),
            ):
                code = main(
                    ["indeterminate-audit", "private-config.json", "--output", str(destination)]
                )

            self.assertEqual(code, 0)
            summary = json.loads(output.getvalue())
            self.assertEqual(summary["total"], 1)
            self.assertNotIn("records", summary)
            self.assertNotIn(job_id, output.getvalue())
            saved = json.loads(destination.read_text())
            self.assertEqual(saved["records"], report["records"])
            self.assertEqual(saved["total"], report["total"])

    def test_resolution_is_idempotent_immutable_and_preserves_job_evidence(self) -> None:
        job_id = self._indeterminate(1, "none", notice=False)
        queued_id = self.fixture.enqueue(message_id=2, payload="private-queued")
        state = HubState.open(self.fixture.config.state_path)
        try:
            before = state.get_provider_job(job_id)
            self.assertTrue(state.resolve_indeterminate_job(job_id, "acknowledged"))
            self.assertFalse(state.resolve_indeterminate_job(job_id, "acknowledged"))
            with self.assertRaises(StateError):
                state.resolve_indeterminate_job(job_id, "superseded")
            with self.assertRaises(StateError):
                state.resolve_indeterminate_job(queued_id, "acknowledged")
            with self.assertRaises(StateError):
                state.resolve_indeterminate_job(job_id, "invalid")
            after = state.get_provider_job(job_id)
            self.assertEqual(
                (after.status, after.error_class, after.error_code, after.error_detail),
                (before.status, before.error_class, before.error_code, before.error_detail),
            )
            self.assertEqual(state.reliability_snapshot()["unresolved_uncertain_execution"], 0)
        finally:
            state.close()

        report = classify_indeterminate_jobs(self.fixture.config.state_path)
        record = next(item for item in report["records"] if item["job_id"] == job_id)
        self.assertEqual(report["resolution_status"], {"resolved": 1})
        self.assertEqual(report["resolutions"], {"acknowledged": 1})
        self.assertEqual(record["resolution"], "acknowledged")
        self.assertIsNotNone(record["resolved_at"])
        self.assertEqual(record["recommended_action"], "none")
        self.assertFalse(report["productive_replay_authorized"])

    def test_cli_resolves_exact_job_without_replay_or_private_content(self) -> None:
        job_id = self._indeterminate(1, "none", notice=False)
        output = io.StringIO()
        with (
            patch(
                "hermes_codex_router.cli.load_external_worker_config",
                return_value=self.fixture.config,
            ),
            redirect_stdout(output),
        ):
            code = main(
                [
                    "indeterminate-resolve",
                    "private-config.json",
                    job_id,
                    "--resolution",
                    "superseded",
                ]
            )

        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["resolution"], "superseded")
        self.assertTrue(result["created"])
        self.assertNotIn("private-", output.getvalue())
        state = HubState.open(self.fixture.config.state_path)
        try:
            job = state.get_provider_job(job_id)
            self.assertEqual(job.status, "indeterminate")
            self.assertEqual(job.attempt_count, 1)
        finally:
            state.close()


if __name__ == "__main__":
    unittest.main()
