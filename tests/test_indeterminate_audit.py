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
from hermes_codex_router.state import HubState


class IndeterminateAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexQueueWorkerTests()
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def _indeterminate(self, message_id: int, phase: str, *, notice: bool) -> str:
        job_id = self.fixture.enqueue(message_id=message_id, payload=f"private-{phase}")
        state = HubState.open(self.fixture.config.state_path)
        try:
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


if __name__ == "__main__":
    unittest.main()
