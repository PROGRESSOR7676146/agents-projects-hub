from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hermes_codex_router.claude_stream import (
    ClaudeStreamError,
    ClaudeTerminalFailure,
    ClaudeVisibleAssistant,
    VisibleAssistantCallback,
)
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.external_runtime import ExternalTurnResult
from hermes_codex_router.root_blockers import persistent_root_blocker
from hermes_codex_router.state import StateError
from tests import test_claude_native_worker as native_fixtures

OTHER_UUID = native_fixtures.OTHER_UUID


class JournalClaudeAdapter:
    runtime = "claude"

    def __init__(self, path: Path, outcome: str = "success") -> None:
        self.path = path
        self.outcome = outcome
        self.calls = 0
        self.observed: tuple[int, str | None, str | None] | None = None

    def run_turn(
        self,
        *,
        session_id: str | None = None,
        new_session_id: str | None = None,
        on_visible_assistant: VisibleAssistantCallback | None = None,
        **_: Any,
    ) -> ExternalTurnResult:
        self.calls += 1
        native = session_id or new_session_id
        assert native is not None
        if on_visible_assistant is not None:
            item_session = OTHER_UUID if self.outcome == "wrong_item" else native
            on_visible_assistant(
                ClaudeVisibleAssistant(item_session, OTHER_UUID, "Saved <partial>")
            )
            # A repeated native message must not create a second journal item.
            on_visible_assistant(
                ClaudeVisibleAssistant(item_session, OTHER_UUID, "Saved <partial>")
            )
        with sqlite3.connect(self.path) as observer:
            self.observed = observer.execute(
                "SELECT (SELECT count(*) FROM provider_visible_items),"
                "completed_text,provider_turn_id FROM provider_execution_checkpoints"
            ).fetchone()
        if self.outcome == "uncertain":
            raise ClaudeStreamError("Fictional incomplete stream")
        if self.outcome == "quota":
            raise ClaudeTerminalFailure(
                "claude_quota_exhausted", "Claude quota rejection; reset time is unknown.", native
            )
        result_session = OTHER_UUID if self.outcome == "wrong_result" else native
        return ExternalTurnResult("claude", "Verified final", result_session, "example-model")


class ClaudeWorkerJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = native_fixtures.ClaudeNativeWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.job_id = self.fixture.enqueue(1)

    def run_worker(self, outcome: str = "success") -> tuple[Any, JournalClaudeAdapter]:
        adapter = JournalClaudeAdapter(self.fixture.path, outcome)
        worker = self.fixture.worker(adapter)  # type: ignore[arg-type]
        worker.run_cycle()
        return worker, adapter

    def assert_incomplete_journal(self, worker: Any, *, items: int) -> None:
        checkpoint = ExecutionJournal(worker.state).read(self.job_id)
        assert checkpoint is not None
        self.assertIsNone(checkpoint["completed_text"])
        self.assertIsNone(checkpoint["provider_turn_id"])
        self.assertEqual(
            worker.state._connection.execute(
                "SELECT count(*) FROM provider_visible_items WHERE job_id=?", (self.job_id,)
            ).fetchone()[0],
            items,
        )
        self.assertEqual(
            worker.state._connection.execute(
                "SELECT count(*) FROM provider_progress_deliveries WHERE job_id=?", (self.job_id,)
            ).fetchone()[0],
            0,
        )
        self.fixture.assert_no_result(worker.state, self.job_id)

    def notice(self, worker: Any) -> str:
        return str(
            worker.state._connection.execute(
                "SELECT telegram_html FROM telegram_outbox WHERE job_id=? AND sender_agent_id='claude'",
                (self.job_id,),
            ).fetchone()[0]
        )

    def test_callback_commits_provisional_item_before_provider_returns_then_completion(
        self,
    ) -> None:
        worker, adapter = self.run_worker()
        self.assertEqual(adapter.observed, (1, None, None))
        checkpoint = ExecutionJournal(worker.state).read(self.job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["completed_text"], "Verified final")
        self.assertIsNone(checkpoint["provider_turn_id"])
        self.assertEqual(worker.state.get_provider_job(self.job_id).status, "result_ready")
        self.assertEqual(
            worker.state.get_provider_result(self.job_id).visible_response, "Verified final"
        )

    def test_uncertain_stream_retains_partial_without_completion_or_replay(self) -> None:
        worker, adapter = self.run_worker("uncertain")
        self.assertEqual(adapter.observed, (1, None, None))
        self.assertEqual(worker.state.get_provider_job(self.job_id).status, "indeterminate")
        self.assert_incomplete_journal(worker, items=1)
        self.assertEqual(
            ExecutionJournal(worker.state).partial_text(self.job_id), "Saved <partial>"
        )
        self.assertIn("Partial response (incomplete)", self.notice(worker))
        self.assertIn("Saved &lt;partial&gt;", self.notice(worker))
        self.assertFalse(worker.run_cycle())
        self.assertEqual(adapter.calls, 1)

    def test_terminal_quota_retains_partial_without_completion(self) -> None:
        worker, adapter = self.run_worker("quota")
        self.assertEqual(adapter.observed, (1, None, None))
        self.assertEqual(worker.state.get_provider_job(self.job_id).status, "failed")
        self.assert_incomplete_journal(worker, items=1)
        self.assertIn("Partial response (incomplete)", self.notice(worker))
        self.assertIn("Saved &lt;partial&gt;", self.notice(worker))

    def test_wrong_final_session_never_records_completion(self) -> None:
        worker, _ = self.run_worker("wrong_result")
        self.assertEqual(worker.state.get_provider_job(self.job_id).status, "indeterminate")
        self.assert_incomplete_journal(worker, items=1)

    def test_wrong_item_session_fails_before_any_partial_is_recorded(self) -> None:
        worker, adapter = self.run_worker("wrong_item")
        self.assertEqual(worker.state.get_provider_job(self.job_id).status, "indeterminate")
        self.assert_incomplete_journal(worker, items=0)
        self.assertNotIn("Saved &lt;partial&gt;", self.notice(worker))
        self.assertNotIn("Partial response (incomplete)", self.notice(worker))
        self.assertFalse(worker.run_cycle())
        self.assertEqual(adapter.calls, 1)

    def test_item_persistence_failure_is_uncertain_and_does_not_replay(self) -> None:
        with patch.object(
            ExecutionJournal,
            "record_claude_item",
            create=True,
            side_effect=StateError("private item persistence diagnosis"),
        ):
            worker, adapter = self.run_worker()
        self.assertEqual(worker.state.get_provider_job(self.job_id).status, "indeterminate")
        self.assert_incomplete_journal(worker, items=0)
        self.assertNotIn("private item persistence diagnosis", self.notice(worker))
        self.assertFalse(worker.run_cycle())
        self.assertEqual(adapter.calls, 1)

    def test_completion_persistence_failure_retains_partial_and_blocks_result(self) -> None:
        with patch.object(
            ExecutionJournal,
            "record_claude_completion",
            create=True,
            side_effect=StateError("private completion persistence diagnosis"),
        ):
            worker, adapter = self.run_worker()
        self.assertEqual(worker.state.get_provider_job(self.job_id).status, "indeterminate")
        self.assert_incomplete_journal(worker, items=1)
        self.assertNotIn("private completion persistence diagnosis", self.notice(worker))
        topic = worker.state.get_provider_job(self.job_id).topic_id
        self.assertIsNotNone(persistent_root_blocker(worker.state._connection, topic_id=topic))
        self.assertFalse(worker.run_cycle())
        self.assertEqual(adapter.calls, 1)

    def test_partial_guard_failure_omits_excerpt_and_retains_uncertainty(self) -> None:
        with patch.object(
            ExecutionJournal,
            "validated_claude_partial",
            create=True,
            side_effect=StateError("private binding diagnosis"),
        ):
            worker, _ = self.run_worker("uncertain")
        self.assertEqual(worker.state.get_provider_job(self.job_id).status, "indeterminate")
        self.assert_incomplete_journal(worker, items=1)
        self.assertNotIn("Saved &lt;partial&gt;", self.notice(worker))
        self.assertNotIn("Partial response (incomplete)", self.notice(worker))
        self.assertNotIn("private binding diagnosis", self.notice(worker))


if __name__ == "__main__":
    unittest.main()
