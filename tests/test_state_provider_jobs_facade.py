from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from hermes_codex_router.state import (
    HubState,
    ProviderChatActivity,
    ProviderJobRecord,
    ProviderJobRecovery,
)
from hermes_codex_router.state_provider_jobs import (
    ProviderChatActivity as FacadeProviderChatActivity,
)
from hermes_codex_router.state_provider_jobs import (
    ProviderJobRecord as FacadeProviderJobRecord,
)
from hermes_codex_router.state_provider_jobs import (
    ProviderJobRecovery as FacadeProviderJobRecovery,
)


class ProviderJobsStateFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tempdir.name) / "private" / "hub.db"
        self.state = HubState.open(self.state_path)

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def topic_session(self, *, project_id: str, thread_id: int, agent_id: str):
        topic = self.state.observe_topic(
            project_id=project_id,
            chat_id=-1001234567890,
            thread_id=thread_id,
            title=f"Fictional topic {thread_id}",
        )
        session = self.state.activate_agent(topic.topic_id, agent_id, "fictional-model", "high")
        return topic, session

    def enqueue(self, topic, session, message_id: int) -> ProviderJobRecord:
        job, created = self.state.enqueue_provider_job(
            idempotency_key=f"facade:{message_id}",
            chat_id=topic.chat_id,
            message_id=message_id,
            topic_id=topic.topic_id,
            agent_id=session.agent_id,
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text=f"fictional request {message_id}",
        )
        self.assertTrue(created)
        return job

    def publish_worker(self, agent_id: str, now: datetime) -> None:
        self.state.upsert_runtime_health(
            component="provider_worker",
            instance_id=f"{agent_id}-worker",
            runtime=agent_id,
            agent_id=agent_id,
            pid=1234,
            process_start_marker=f"{agent_id}-start",
            started_at=now,
            heartbeat_at=now,
        )

    def test_facade_uses_hub_connection_and_preserves_public_record_types(self) -> None:
        self.assertIs(self.state._provider_job_state._connection, self.state._connection)
        self.assertEqual(
            self.state._provider_job_state._transaction,
            self.state._immediate_transaction,
        )
        self.assertEqual(
            self.state._provider_job_state._write_transaction,
            self.state._connection_transaction,
        )
        self.assertIs(ProviderJobRecord, FacadeProviderJobRecord)
        self.assertIs(ProviderJobRecovery, FacadeProviderJobRecovery)
        self.assertIs(ProviderChatActivity, FacadeProviderChatActivity)

        topic, session = self.topic_session(
            project_id="example-project", thread_id=71, agent_id="codex"
        )
        job = self.enqueue(topic, session, 701)

        self.assertIsInstance(self.state.get_provider_job(job.job_id), ProviderJobRecord)
        self.assertEqual(self.state.provider_jobs_for_topic(topic.topic_id), (job,))

    def test_lease_fault_rolls_back_through_hub_transaction_owner(self) -> None:
        topic, session = self.topic_session(
            project_id="example-project", thread_id=72, agent_id="codex"
        )
        job = self.enqueue(topic, session, 702)
        transaction = self.state._provider_job_state._transaction

        @contextmanager
        def fail_after_lease() -> Iterator[None]:
            with transaction():
                yield
                raise RuntimeError("fictional post-lease fault")

        with patch.object(self.state._provider_job_state, "_transaction", fail_after_lease):
            with self.assertRaisesRegex(RuntimeError, "post-lease fault"):
                self.state.lease_provider_job("codex", "fictional-worker")

        restored = self.state.get_provider_job(job.job_id)
        self.assertEqual(restored.status, "queued")
        self.assertIsNone(restored.lease_owner)
        self.assertIsNone(restored.lease_token)
        self.assertFalse(self.state._connection.in_transaction)

    def test_single_write_fault_rolls_back_through_hub_transaction_owner(self) -> None:
        topic, session = self.topic_session(
            project_id="example-project", thread_id=75, agent_id="codex"
        )
        job = self.enqueue(topic, session, 705)
        transaction = self.state._provider_job_state._write_transaction

        @contextmanager
        def fail_after_write() -> Iterator[None]:
            with transaction():
                yield
                raise RuntimeError("fictional post-write fault")

        with patch.object(
            self.state._provider_job_state,
            "_write_transaction",
            fail_after_write,
        ):
            with self.assertRaisesRegex(RuntimeError, "post-write fault"):
                self.state.cancel_provider_job(job.job_id)

        self.assertEqual(self.state.get_provider_job(job.job_id).status, "queued")
        self.assertFalse(self.state._connection.in_transaction)

    def test_fair_scheduler_and_stale_recovery_remain_on_public_surface(self) -> None:
        clock = datetime.now(timezone.utc)
        agents = ("opencode", "antigravity")
        first_topic, first_session = self.topic_session(
            project_id="example-project-a", thread_id=73, agent_id=agents[0]
        )
        second_topic, second_session = self.topic_session(
            project_id="example-project-b", thread_id=74, agent_id=agents[1]
        )
        first = self.enqueue(first_topic, first_session, 703)
        second = self.enqueue(second_topic, second_session, 704)
        for agent_id in agents:
            self.publish_worker(agent_id, clock)

        self.assertIsNone(
            self.state.lease_provider_job(
                agents[1],
                "antigravity-worker",
                scheduler_agents=agents,
                now=clock,
            )
        )
        first_lease = self.state.lease_provider_job(
            agents[0], "opencode-worker", scheduler_agents=agents, now=clock
        )
        assert first_lease is not None and first_lease.lease_token is not None
        self.assertEqual(first_lease.job_id, first.job_id)
        self.state.release_provider_job_lease(first_lease.job_id, first_lease.lease_token)

        second_lease = self.state.lease_provider_job(
            agents[1],
            "antigravity-worker",
            lease_seconds=1,
            scheduler_agents=agents,
            now=clock,
        )
        assert second_lease is not None and second_lease.lease_token is not None
        self.assertEqual(second_lease.job_id, second.job_id)
        self.state.mark_provider_job_executing(
            second_lease.job_id, second_lease.lease_token, now=clock
        )

        recovery = self.state.recover_stale_provider_jobs(
            agent_id=agents[1], now=clock + timedelta(seconds=2)
        )
        self.assertIsInstance(recovery, ProviderJobRecovery)
        self.assertEqual(recovery.indeterminate_job_ids, (second.job_id,))
        self.assertEqual(self.state.get_provider_job(second.job_id).status, "indeterminate")


if __name__ == "__main__":
    unittest.main()
