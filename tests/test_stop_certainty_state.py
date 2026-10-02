from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.root_blockers import persistent_root_blocker
from hermes_codex_router.state import HubState


class StopCertaintyStateTests(unittest.TestCase):
    def test_pending_stop_preserves_unknown_execution_and_independent_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = HubState.open(Path(directory) / "state.db")
            self.addCleanup(state.close)
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=7,
                title="Example topic",
            )
            session = state.activate_agent(topic.topic_id, "codex", "example-model", "high")
            job, _ = state.enqueue_provider_job(
                idempotency_key="example-job",
                chat_id=topic.chat_id,
                message_id=1,
                topic_id=topic.topic_id,
                agent_id="codex",
                session_id=session.session_id,
                session_generation=session.generation,
                model=session.model,
                effort=session.effort,
                payload_text="Example task",
            )
            leased = state.lease_provider_job("codex", "example-worker")
            assert leased is not None and leased.lease_token is not None
            state.mark_provider_job_executing(job.job_id, leased.lease_token)
            request_id, _, _ = state.request_emergency_stop(
                topic_id=topic.topic_id,
                chat_id=topic.chat_id,
                message_id=2,
                target_agent_id="codex",
            )
            state.enqueue_emergency_stop_notice(request_id, "Stop requested; outcome pending.")
            result = state.terminate_provider_job_with_notice(
                job.job_id,
                leased.lease_token,
                status="indeterminate",
                error_class="ambiguous_execution",
                error_code="transport_lost",
                sender_agent_id="codex",
                telegram_html="Outcome unknown; root remains paused.",
            )
            self.assertEqual(result.status, "indeterminate")
            self.assertIsNotNone(
                persistent_root_blocker(state._connection, topic_id=topic.topic_id)
            )
            notice = state._connection.execute(
                "SELECT sender_agent_id FROM telegram_outbox WHERE job_id=?", (job.job_id,)
            ).fetchone()
            self.assertEqual(notice[0], "codex")
            self.assertEqual(state.pending_emergency_stop_for_job(job.job_id), request_id)


if __name__ == "__main__":
    unittest.main()
