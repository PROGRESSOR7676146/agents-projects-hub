"""Retained Claude leases cannot enter unsupported native transfer paths."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.hub_config import AgentDefinition
from tests.hub_service_harness import ANTIGRAVITY, CODEX, HubHarness

NATIVE_UUID = "00000000-0000-4000-8000-000000000001"
CLAUDE = replace(
    ANTIGRAVITY,
    agent_id="claude",
    runtime="claude",
    display_name="Claude",
    telegram_username="example_claude_bot",
    default_model="example-claude",
)


class ClaudeTransferRefusalTests(unittest.TestCase):
    def harness(
        self, *, agent: AgentDefinition = CLAUDE, dispatch_mode: str = "queue"
    ) -> HubHarness:
        temporary = tempfile.TemporaryDirectory(prefix="example-claude-transfer-")
        self.addCleanup(temporary.cleanup)
        hub = HubHarness(Path(temporary.name), agents=(CODEX, agent), dispatch_mode=dispatch_mode)
        self.addCleanup(hub.close)
        return hub

    @staticmethod
    def retained_snapshot(hub: HubHarness) -> dict[str, list[tuple[object, ...]]]:
        return {
            table: [
                tuple(row)
                for row in hub.service.state._connection.execute(f"SELECT * FROM {table}")
            ]
            for table in (
                "agent_sessions",
                "provider_jobs",
                "provider_job_holds",
                "provider_execution_checkpoints",
                "provider_job_results",
            )
        }

    def test_claude_and_alias_transfer_refuse_before_preparation_or_lease_mutation(self) -> None:
        alias = replace(CLAUDE, agent_id="reviewer", telegram_username="example_reviewer_bot")
        for agent in (CLAUDE, alias):
            for mode in ("queue", "inline"):
                for command, writer in (("/local", "telegram"), ("/return", "local")):
                    with self.subTest(agent=agent.agent_id, mode=mode, command=command):
                        hub = self.harness(agent=agent, dispatch_mode=mode)
                        saved = hub.activate(
                            agent, provider_session_id=NATIVE_UUID, writer_mode=writer
                        )
                        before = self.retained_snapshot(hub)
                        with ExitStack() as stack:
                            for owner, name in (
                                (type(hub.service.state), "writer_transfer_snapshot"),
                                (type(hub.service.state), "set_writer_mode"),
                                (type(hub.service), "_enqueue_provider_turn"),
                            ):
                                stack.enter_context(
                                    patch.object(
                                        owner,
                                        name,
                                        side_effect=AssertionError("transfer boundary crossed"),
                                    )
                                )
                            stack.enter_context(
                                patch(
                                    "hermes_codex_router.service.local_resume_command",
                                    side_effect=AssertionError("resume prepared"),
                                )
                            )
                            self.assertTrue(hub.send(command, message_id=101))
                        self.assertIn(
                            "Claude native session transfer is not supported", hub.last_reply
                        )
                        self.assertIn("did not invoke the provider", hub.last_reply)
                        self.assertEqual(hub.session(), saved)
                        self.assertEqual(self.retained_snapshot(hub), before)
                        self.assertEqual(hub.client.turns, 0)

    def test_retained_local_return_claims_duplicate_control_once(self) -> None:
        hub = self.harness()
        saved = hub.activate(CLAUDE, provider_session_id=NATIVE_UUID, writer_mode="local")
        sent_before = len(hub.telegram.sent)
        self.assertTrue(hub.send("/return", message_id=101))
        self.assertFalse(hub.send("/return", message_id=101))
        self.assertEqual(len(hub.telegram.sent), sent_before + 1)
        self.assertTrue(hub.service.state.message_already_observed(hub.topic().chat_id, 101))
        self.assertTrue(hub.send("/return", message_id=102))
        self.assertEqual(len(hub.telegram.sent), sent_before + 2)
        self.assertEqual(hub.session(), saved)
        self.assertEqual(hub.service.state.provider_jobs_for_topic(hub.topic().topic_id), ())

    def test_missing_native_session_does_not_suggest_productive_work_will_enable_transfer(
        self,
    ) -> None:
        hub = self.harness()
        saved = hub.activate(CLAUDE, provider_session_id=None)
        self.assertTrue(hub.send("/local"))
        self.assertIn("not supported", hub.last_reply)
        self.assertNotIn("productive turn first", hub.last_reply)
        self.assertEqual(hub.session(), saved)

    def test_retained_local_lease_and_earlier_hold_remain_untouched(self) -> None:
        hub = self.harness()
        saved = hub.activate(CLAUDE, provider_session_id=NATIVE_UUID)
        job, _ = hub.service.state.enqueue_provider_job(
            idempotency_key="example-held-transfer",
            chat_id=hub.topic().chat_id,
            message_id=90,
            topic_id=hub.topic().topic_id,
            agent_id=saved.agent_id,
            session_id=saved.session_id,
            session_generation=saved.generation,
            model=saved.model,
            effort=saved.effort,
            payload_text="Fictional earlier authorized task",
        )
        # Retained/inconsistent state is the target; normal /local cannot create it.
        with hub.service.state._connection:
            hub.service.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
                (saved.session_id,),
            )
            hub.service.state._connection.execute(
                "INSERT INTO provider_job_holds (job_id, cause_job_id, held_at) VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
                (job.job_id, job.job_id),
            )
        before = self.retained_snapshot(hub)
        self.assertTrue(hub.send("/return", message_id=101))
        self.assertIn("not supported", hub.last_reply)
        self.assertEqual(self.retained_snapshot(hub), before)

    def test_agent_name_claude_does_not_block_supported_runtime(self) -> None:
        other = replace(CLAUDE, runtime="antigravity")
        hub = self.harness(agent=other)
        hub.activate(other, provider_session_id="example-conversation")
        self.assertTrue(hub.send("/local"))
        self.assertEqual(hub.session().writer_mode, "local")
        self.assertIn("--conversation example-conversation", hub.last_reply)
        self.assertTrue(hub.send("/return"))
        jobs = hub.service.state.provider_jobs_for_topic(hub.topic().topic_id)
        self.assertEqual(len(jobs), 1)
        self.assertIn("Summarize only", jobs[0].payload_text)
