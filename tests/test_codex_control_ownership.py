"""Control ownership follows the saved root beyond ordinary job terminality."""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from hermes_codex_router.codex_control_predicates import resolved_control_scope
from hermes_codex_router.controller_admission import (
    DurableAdmissionRequest,
    DurableProviderAdmission,
    RejectedAdmission,
)
from hermes_codex_router.hub_config import HubTelegramBot
from hermes_codex_router.outbox_sender import TelegramOutboxSender
from hermes_codex_router.root_blockers import persistent_root_blocker
from hermes_codex_router.session_adoption_state import AdoptionRequest, CodexSessionOrigins
from hermes_codex_router.state import StateError
from hermes_codex_router.telegram import TopicMessage
from tests import test_codex_turn_controls as fixtures
from tests.test_controller_admission import FakeDownloadTransport
from tests.test_outbox_sender import Bot


class CodexControlOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexTurnControlJournalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.job_id = self.fixture.job_id
        self.journal = self.fixture.journal
        self.job = self.state.get_provider_job(self.job_id)
        self.root = self.fixture.root
        self.journal.record_turn(self.job_id, self.fixture.token, "example-turn")

    def peer(self, *, root=None, legacy: bool = False):
        topic = self.state.observe_topic(
            project_id="example-alias",
            chat_id=self.job.chat_id,
            thread_id=177,
            title="Fictional alias",
            execution_root=None if legacy else (root or self.root),
        )
        session = self.state.activate_agent(topic.topic_id, "codex", "fictional", "high")
        return topic, session

    def enqueue(self, topic, session, *, message_id=77):
        return self.state.enqueue_provider_job(
            idempotency_key=f"example-peer-{message_id}",
            chat_id=topic.chat_id,
            message_id=message_id,
            topic_id=topic.topic_id,
            agent_id=session.agent_id,
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text="Fictional independent request",
        )[0]

    def owner_with_terminal_job(self) -> None:
        with self.state._immediate_transaction():
            self.state._connection.execute(
                """UPDATE codex_turn_controls SET send_started_at=?,send_owner_token_hash=?,
                   interrupt_source='protective' WHERE job_id=?""",
                (datetime.now(timezone.utc).isoformat(), "example-send-owner", self.job_id),
            )
            self.state._connection.execute(
                """UPDATE provider_jobs SET status='completed',lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL WHERE job_id=?""",
                (self.job_id,),
            )

    def test_same_root_alias_cannot_be_leased_after_terminal_job(self) -> None:
        topic, session = self.peer()
        self.enqueue(topic, session)
        self.owner_with_terminal_job()
        self.assertIsNone(self.state.lease_provider_job("codex", "example-worker"))

    def test_new_owner_between_lease_and_execution_prevents_productive_start(self) -> None:
        topic, session = self.peer()
        peer = self.enqueue(topic, session)
        with self.state._immediate_transaction():
            self.state._connection.execute(
                """UPDATE provider_jobs SET status='completed',lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL WHERE job_id=?""",
                (self.job_id,),
            )
        leased = self.state.lease_provider_job("codex", "example-worker")
        assert leased is not None
        self.assertEqual(leased.job_id, peer.job_id)
        self.owner_with_terminal_job()
        assert leased.lease_token is not None
        with self.assertRaises(StateError):
            self.state.mark_provider_job_executing(leased.job_id, leased.lease_token)

    def test_unrelated_canonical_root_is_eligible_despite_control_owner(self) -> None:
        topic, session = self.peer(root=self.root.parent / "example-independent")
        peer = self.enqueue(topic, session)
        self.owner_with_terminal_job()
        leased = self.state.lease_provider_job("codex", "example-worker")
        assert leased is not None
        self.assertEqual(leased.job_id, peer.job_id)

    def test_resolvable_legacy_peer_on_another_root_remains_eligible(self) -> None:
        topic, session = self.peer(legacy=True)
        other_root = self.root.parent / "example-independent"
        with self.state._immediate_transaction():
            self.state._connection.execute(
                """INSERT INTO codex_session_origins
                   (session_id,provider_thread_id,project_id,canonical_root,model_provider,created_at,activation_message_id)
                   VALUES (?,?,?,?, 'openai','example-time',1)""",
                (session.session_id, "example-peer-thread", topic.project_id, str(other_root)),
            )
        self.owner_with_terminal_job()
        peer = self.enqueue(topic, session)
        leased = self.state.lease_provider_job("codex", "example-worker")
        self.assertIsNotNone(leased)
        assert leased is not None
        self.assertEqual(leased.job_id, peer.job_id)

    def test_fresh_legacy_admission_works_only_when_global_control_owner_set_is_empty(self) -> None:
        topic, session = self.peer(legacy=True)
        with self.state._immediate_transaction():
            self.assertIsNone(
                persistent_root_blocker(self.state._connection, topic_id=topic.topic_id)
            )
        self.owner_with_terminal_job()
        with self.assertRaisesRegex(StateError, "persistent local writer or uncertainty"):
            self.enqueue(topic, session)
        notice = self.state.reject_blocked_provider_input(
            chat_id=topic.chat_id,
            message_id=78,
            topic_id=topic.topic_id,
            session_id=session.session_id,
            session_generation=session.generation,
        )
        assert notice is not None
        self.assertEqual(notice.blocker_kind, "scope_unconfirmed")
        self.assertIn("корень", notice.telegram_html)
        self.assertNotIn("теме-владельце", notice.telegram_html)

    def test_control_owner_appearing_after_preflight_has_durable_input_refusal(self) -> None:
        self.assert_admission_race_refused(legacy=False)

    def test_unresolved_scope_appearing_after_preflight_has_durable_input_refusal(self) -> None:
        self.assert_admission_race_refused(legacy=True)

    def test_batched_control_owner_race_has_durable_input_refusal(self) -> None:
        self.assert_admission_race_refused(legacy=False, batched=True)

    def test_batched_unresolved_scope_race_has_durable_input_refusal(self) -> None:
        self.assert_admission_race_refused(legacy=True, batched=True)

    def assert_admission_race_refused(self, *, legacy: bool, batched: bool = False) -> None:
        topic, session = self.peer(legacy=legacy)
        admission = DurableProviderAdmission(
            state=self.state,
            telegram=FakeDownloadTransport({}),
            state_path=self.fixture.fixture.config.state_path,
            observer_agent_id="hub",
            message_batch_quiet_ms=1500,
            message_batch_max_ms=8000,
        )
        request = DurableAdmissionRequest(
            message=TopicMessage(
                update_id=77,
                message_id=77,
                chat_id=topic.chat_id,
                thread_id=topic.thread_id,
                chat_title=topic.title,
                sender_id=42,
                text="Example blocked request",
            ),
            topic=topic,
            session=session,
            prompt="Example blocked request",
            batchable_user_text="Example blocked request" if batched else None,
        )
        method = "enqueue_or_append_provider_job" if batched else "enqueue_provider_job"
        original = getattr(self.state, method)

        def race(**kwargs):
            self.owner_with_terminal_job()
            return original(**kwargs)

        with patch.object(self.state, method, side_effect=race):
            result = admission.admit(request)
        self.assertEqual(result, RejectedAdmission("persistent_root_blocker"))
        self.assertTrue(self.state.message_already_observed(topic.chat_id, 77))
        self.assertEqual(self.state.provider_jobs_for_topic(topic.topic_id), ())
        notice = self.state.lease_root_blocker_notice("example-sender")
        assert notice is not None
        self.assertEqual(notice.blocker_kind, "scope_unconfirmed" if legacy else "control")
        self.assertEqual(notice.reply_to_message_id, 77)
        independent = self.state.observe_topic(
            project_id="example-independent",
            chat_id=topic.chat_id,
            thread_id=178,
            title="Example independent topic",
            execution_root=self.root.parent / "example-independent",
        )
        other_session = self.state.activate_agent(
            independent.topic_id, "codex", "fictional", "high"
        )
        self.assertIsNotNone(self.enqueue(independent, other_session, message_id=78))

    def test_held_control_notice_names_observation_without_writer_release(self) -> None:
        topic, session = self.peer()
        self.enqueue(topic, session)
        self.owner_with_terminal_job()
        self.assertEqual(self.state.materialize_held_provider_jobs(), 1)
        notice = self.state.lease_root_blocker_notice("example-sender")
        assert notice is not None
        self.assertIn("управления", notice.telegram_html)
        self.assertNotIn("Освободить проект", str(notice.reply_markup))

    def test_unresolved_queued_scope_is_held_without_aborting_unrelated_sender_work(self) -> None:
        topic, session = self.peer(legacy=True)
        peer = self.enqueue(topic, session)
        self.owner_with_terminal_job()
        self.assertEqual(self.state.materialize_held_provider_jobs(), 1)
        self.assertEqual(self.state.held_provider_job_count(topic.topic_id), 1)
        notice = self.state.lease_root_blocker_notice("example-sender")
        assert notice is not None
        self.assertEqual(notice.blocker_kind, "scope_unconfirmed")
        self.assertIn("корень", notice.telegram_html)
        self.assertEqual(self.state.get_provider_job(peer.job_id).attempt_count, 0)
        self.assertEqual(self.state.materialize_released_uncertainty_notices(), 0)
        self.state.complete_root_blocker_notice(notice, 903)
        with self.assertRaisesRegex(StateError, "topic root is unconfirmed"):
            self.state.decide_held_provider_job(
                job_id=peer.job_id,
                action="confirm",
                chat_id=topic.chat_id,
                thread_id=topic.thread_id,
                notice_message_id=903,
            )
        self.assertEqual(self.state.get_provider_job(peer.job_id).attempt_count, 0)

    def test_ambiguous_legacy_scope_cannot_abort_unrelated_final_delivery(self) -> None:
        topic, session = self.peer(legacy=True)
        other_session = self.state.activate_agent(
            topic.topic_id, "example-other", "fictional", "high"
        )
        peer = self.enqueue(topic, session)
        with self.state._immediate_transaction():
            for index, origin_session in enumerate((session, other_session)):
                self.state._connection.execute(
                    """INSERT INTO codex_session_origins
                       (session_id,provider_thread_id,project_id,canonical_root,model_provider,created_at,activation_message_id)
                       VALUES (?,?,?,?, 'openai','example-time',1)""",
                    (
                        origin_session.session_id,
                        f"example-old-thread-{index}",
                        topic.project_id,
                        str(self.root.parent / f"example-old-root-{index}"),
                    ),
                )
        independent = self.state.observe_topic(
            project_id="example-independent",
            chat_id=self.job.chat_id,
            thread_id=178,
            title="Example independent",
            execution_root=self.root.parent / "example-independent",
        )
        selected = self.state.activate_agent(independent.topic_id, "codex", "fictional", "high")
        job, _ = self.state.enqueue_provider_job(
            idempotency_key="example-independent-input",
            chat_id=independent.chat_id,
            message_id=80,
            topic_id=independent.topic_id,
            agent_id="codex",
            session_id=selected.session_id,
            session_generation=selected.generation,
            model=selected.model,
            effort=selected.effort,
            payload_text="Example independent task",
        )
        self.owner_with_terminal_job()
        leased = self.state.lease_provider_job("codex", "example-independent-worker")
        assert leased is not None and leased.lease_token is not None
        self.assertEqual(leased.job_id, job.job_id)
        self.state.mark_provider_job_executing(job.job_id, leased.lease_token)
        self.state.commit_provider_result(
            job.job_id,
            leased.lease_token,
            visible_response="Example independent final",
            sender_agent_id="codex",
            telegram_html="Example independent final",
        )
        config = replace(
            self.fixture.fixture.config,
            outbox_runtime="external",
            hub_bot=HubTelegramBot("example_hub_bot", self.root.parent / "unused-token"),
        )
        bots = {"hub": Bot(), "codex": Bot()}
        sender = TelegramOutboxSender(config, telegram_bots=bots)
        try:
            for _ in range(3):
                sender.run_cycle()
            self.assertEqual(self.state.get_provider_job(job.job_id).status, "completed")
            self.assertEqual(self.state.get_provider_job(peer.job_id).attempt_count, 0)
            self.assertEqual(self.state.held_provider_job_count(topic.topic_id), 1)
            self.assertEqual(
                bots["codex"].sent,
                [(independent.chat_id, independent.thread_id, "Example independent final")],
            )
            self.assertIn("корень", bots["hub"].sent[0][2])
        finally:
            sender.close()

    def test_same_root_control_blocker_has_its_own_truthful_notice_kind(self) -> None:
        topic, session = self.peer()
        self.owner_with_terminal_job()
        notice = self.state.reject_blocked_provider_input(
            chat_id=topic.chat_id,
            message_id=77,
            topic_id=topic.topic_id,
            session_id=session.session_id,
            session_generation=session.generation,
        )
        assert notice is not None
        self.assertEqual(notice.blocker_kind, "control")
        self.assertIn("управления", notice.telegram_html)
        self.assertNotIn("/release", notice.telegram_html)
        self.assertEqual(
            self.state._connection.execute(
                "SELECT control_job_id FROM hub_blocker_outbox WHERE outbox_id=?",
                (notice.outbox_id,),
            ).fetchone()[0],
            self.job_id,
        )

    def test_completed_control_owner_counts_for_drain_but_not_raw_work(self) -> None:
        self.owner_with_terminal_job()
        self.assertEqual(self.state.nonterminal_provider_job_counts(("codex",)), {})
        self.assertEqual(
            self.state.effective_nonterminal_provider_job_counts(("codex",)), {"codex": 1}
        )

    def test_controlled_session_identity_cannot_be_rebound_or_replaced(self) -> None:
        self.owner_with_terminal_job()
        with self.assertRaises(StateError):
            self.state.bind_provider_session(self.job.session_id, "example-other-thread", None)
        with self.assertRaises(StateError):
            self.state.new_active_session(self.job.topic_id)

    def test_resolver_uses_saved_target_root_without_current_filesystem_access(self) -> None:
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE topics SET execution_scope='project:example-project' WHERE topic_id=?",
                (self.job.topic_id,),
            )
            self.assertEqual(
                resolved_control_scope(self.state._connection, self.job.topic_id),
                f"root:{self.root}",
            )

    def test_inline_dispatch_cannot_bypass_terminal_job_control_owner(self) -> None:
        self.owner_with_terminal_job()
        with self.assertRaises(StateError):
            self.state.start_dispatch(
                chat_id=self.job.chat_id,
                message_id=888,
                topic_id=self.job.topic_id,
                agent_id="codex",
            )

    def test_already_attached_adoption_cannot_bypass_control_owner(self) -> None:
        self.owner_with_terminal_job()
        topic = self.state.get_topic(self.job.topic_id)
        session = self.state.get_session(self.job.session_id)
        with self.assertRaises(StateError):
            CodexSessionOrigins(self.state).preview(
                AdoptionRequest(
                    project_id=topic.project_id,
                    chat_id=topic.chat_id,
                    thread_id=topic.thread_id,
                    provider_thread_id="example-thread",
                    canonical_root=self.root,
                    model=session.model,
                    effort=session.effort,
                    replaces_session_id=session.session_id,
                )
            )

    def test_unbound_lane_archive_checks_saved_root_control_owner(self) -> None:
        self.state.register_lane(
            lane_id="example-lane",
            project_id="example-project",
            worktree_path=self.root,
            branch_name="example-branch",
        )
        self.owner_with_terminal_job()
        with self.assertRaises(StateError):
            self.state.archive_lane("example-lane")
        self.assertEqual(self.state.get_lane("example-lane")["status"], "active")

    def test_archived_lane_cleanup_preflight_checks_owner_before_filesystem_mutation(self) -> None:
        self.state.register_lane(
            lane_id="example-lane",
            project_id="example-project",
            worktree_path=self.root,
            branch_name="example-branch",
        )
        self.state.archive_lane("example-lane")
        self.owner_with_terminal_job()
        with self.assertRaises(StateError):
            self.state.require_lane_cleanup("example-lane")
        self.assertIsNone(self.state.get_lane("example-lane")["cleaned_at"])


if __name__ == "__main__":
    unittest.main()
