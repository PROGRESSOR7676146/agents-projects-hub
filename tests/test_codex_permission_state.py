from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

from hermes_codex_router.codex_session_adoption import open_adoption_state
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.outbox_sender import TelegramOutboxSender
from hermes_codex_router.session_adoption_state import AdoptionRequest, CodexSessionOrigins
from hermes_codex_router.session_connect import ConnectCandidate, SessionConnectStore
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.turn_continuation_state import TurnContinuationState
from tests import test_outbox_sender as sender_fixtures

PROFILE = "example-project-policy"


class CodexPermissionStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "state.db"
        self.state = HubState.open(self.path, codex_permission_profile=PROFILE)
        self.addCleanup(self.state.close)
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=1,
            title="Example topic",
            execution_root=self.root,
        )

    def activate(self, state: HubState | None = None):
        return (self.state if state is None else state).activate_agent(
            self.topic.topic_id, "codex", "example-model", "low"
        )

    def enqueue(self, state: HubState | None = None, *, message: int = 1):
        owner = self.state if state is None else state
        session = owner.active_session(self.topic.topic_id)
        assert session is not None
        return owner.enqueue_provider_job(
            idempotency_key=f"example-input:{message}",
            chat_id=self.topic.chat_id,
            message_id=message,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text="Example task",
        )[0]

    def test_session_and_job_selection_survive_restart_as_immutable_snapshots(self) -> None:
        session = self.activate()
        job = self.enqueue()
        with closing(HubState.open(self.path, codex_permission_profile=PROFILE)) as restarted:
            self.assertEqual(
                restarted.get_session(session.session_id).codex_permission_profile, PROFILE
            )
            self.assertEqual(
                restarted.get_provider_job(job.job_id).codex_permission_profile, PROFILE
            )
        for table, key, value in (
            ("agent_sessions", "session_id", session.session_id),
            ("provider_jobs", "job_id", job.job_id),
        ):
            with self.subTest(table=table), self.assertRaises(sqlite3.IntegrityError):
                with self.state._connection:
                    self.state._connection.execute(
                        f"UPDATE {table} SET codex_permission_profile=NULL WHERE {key}=?", (value,)
                    )

    def test_changed_or_omitted_context_never_retargets_existing_session_or_job(self) -> None:
        session = self.activate()
        job = self.enqueue()
        for kwargs in (
            {},
            {"codex_permission_profile": None},
            {"codex_permission_profile": "example-other-policy"},
        ):
            with (
                self.subTest(kwargs=kwargs),
                closing(HubState.open(self.path, **kwargs)) as changed,
            ):
                self.assertEqual(
                    changed.get_session(session.session_id).codex_permission_profile, PROFILE
                )
                with self.assertRaises(StateError):
                    self.activate(changed)
                with self.assertRaises(StateError):
                    self.enqueue(changed, message=2)
                self.assertEqual(
                    changed.get_provider_job(job.job_id).codex_permission_profile, PROFILE
                )

    def test_missing_context_refuses_codex_creation_but_explicit_legacy_is_supported(self) -> None:
        with closing(HubState.open(self.path)) as unknown:
            with self.assertRaises(StateError):
                self.activate(unknown)
            self.assertIsNone(unknown.active_session(self.topic.topic_id))
            other = unknown.ensure_satellite(self.topic.topic_id, "claude", "example-model", "low")
            self.assertIsNone(other.codex_permission_profile)
        with closing(HubState.open(self.path, codex_permission_profile=None)) as legacy:
            self.assertIsNone(self.activate(legacy).codex_permission_profile)

    def test_new_is_explicit_migration_while_model_changes_preserve_selection(self) -> None:
        original = self.activate()
        replaced = self.state.replace_active_session(
            self.topic.topic_id, model="example-other-model", effort="high"
        )
        self.assertEqual(replaced.codex_permission_profile, PROFILE)
        with closing(
            HubState.open(self.path, codex_permission_profile="example-other-policy")
        ) as changed:
            with self.assertRaises(StateError):
                changed.replace_active_session(
                    self.topic.topic_id, model="example-model", effort="low"
                )
            new = changed.new_active_session(self.topic.topic_id)
            self.assertEqual(new.codex_permission_profile, "example-other-policy")
            self.assertEqual(
                changed.get_session(original.session_id).codex_permission_profile, PROFILE
            )

    def test_control_activation_preserves_stale_satellite_until_explicit_new(self) -> None:
        original = self.activate()
        claude = self.state.activate_agent(self.topic.topic_id, "claude", "example-model", "low")
        with closing(
            HubState.open(self.path, codex_permission_profile="example-next-policy")
        ) as changed:
            selected = changed.activate_agent(
                self.topic.topic_id,
                "codex",
                "example-other-model",
                "high",
                expected_session_id=claude.session_id,
                control_only=True,
            )
            self.assertEqual(selected.session_id, original.session_id)
            self.assertEqual(
                (selected.model, selected.effort, selected.codex_permission_profile),
                (original.model, original.effort, PROFILE),
            )
            with self.assertRaises(StateError):
                self.enqueue(changed, message=2)
            new = changed.new_active_session(
                self.topic.topic_id, expected_session_id=selected.session_id
            )
            self.assertEqual(new.codex_permission_profile, "example-next-policy")
            self.assertEqual(
                self.enqueue(changed, message=2).codex_permission_profile, "example-next-policy"
            )

    def test_reset_and_model_replacement_cannot_drop_a_local_writer(self) -> None:
        session = self.activate()
        for mode in ("local", "terminal"):
            with self.subTest(mode=mode):
                with self.state._connection:
                    self.state._connection.execute(
                        "UPDATE agent_sessions SET writer_mode=? WHERE session_id=?",
                        (mode, session.session_id),
                    )
                for operation in (
                    lambda: self.state.new_active_session(self.topic.topic_id),
                    lambda: self.state.replace_active_session(
                        self.topic.topic_id, model="example-other", effort="high"
                    ),
                ):
                    with self.assertRaisesRegex(StateError, "return the local writer"):
                        operation()
                    active = self.state.active_session(self.topic.topic_id)
                    assert active is not None
                    self.assertEqual(
                        (active.session_id, active.writer_mode), (session.session_id, mode)
                    )

    def test_checkpoint_requires_server_confirmed_job_and_session_selection(self) -> None:
        self.activate()
        job = self.enqueue()
        leased = self.state.lease_provider_job("codex", "example-worker")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, leased.lease_token)
        journal = ExecutionJournal(self.state)
        for actual in (None, "example-other-policy"):
            with self.subTest(actual=actual), self.assertRaises(StateError):
                journal.record_thread(
                    job.job_id,
                    leased.lease_token,
                    "example-thread",
                    self.root,
                    codex_permission_profile=actual,
                )
            self.assertIsNone(journal.read(job.job_id))
        journal.record_thread(
            job.job_id,
            leased.lease_token,
            "example-thread",
            self.root,
            codex_permission_profile=PROFILE,
        )
        checkpoint = journal.read(job.job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["codex_permission_profile"], PROFILE)

    def prepare_marker(self):
        store = SessionConnectStore(self.state)
        workflow = store.start_topic(
            owner_user_id=42,
            project_id="example-project",
            canonical_root=self.root,
            chat_id=self.topic.chat_id,
            thread_id=self.topic.thread_id,
            model="example-model",
            effort="low",
            source=ConnectCandidate(
                "example-candidate", "example-saved-thread", "Example saved session", 1
            ),
        )
        self.assertEqual(workflow.codex_permission_profile, PROFILE)
        store.request_activation(42, workflow.workflow_id)
        leased = store.lease_worker("example-worker")
        assert leased is not None
        store.prepare_marker(leased.workflow_id, leased.lease_token)
        outbox = store.lease_outbox("example-sender")
        assert outbox is not None
        return store, workflow, outbox

    def test_connect_selection_reaches_new_generation_without_claiming_source_policy(self) -> None:
        store, workflow, outbox = self.prepare_marker()
        self.assertFalse(store.refuse_changed_marker_selection(outbox))
        completed = store.complete_marker(outbox, telegram_message_id=120)
        self.assertEqual(completed.codex_permission_profile, PROFILE)
        session = self.state.active_session(self.topic.topic_id)
        assert session is not None
        self.assertEqual(session.codex_permission_profile, PROFILE)
        self.assertEqual(session.provider_session_id, "example-saved-thread")

    def test_connect_sender_policy_drift_refuses_marker_before_any_activation(self) -> None:
        store, workflow, outbox = self.prepare_marker()
        with closing(HubState.open(self.path, codex_permission_profile=None)) as changed:
            changed_store = SessionConnectStore(changed)
            self.assertTrue(changed_store.refuse_changed_marker_selection(outbox))
            self.assertEqual(changed_store.get(workflow.workflow_id).stage, "failed")
            self.assertIsNone(changed.active_session(self.topic.topic_id))
            notices = changed._connection.execute(
                "SELECT kind FROM session_connect_outbox WHERE workflow_id=? AND kind='notice'",
                (workflow.workflow_id,),
            ).fetchall()
            self.assertEqual(len(notices), 1)
        self.assertEqual(store.get(workflow.workflow_id).codex_permission_profile, PROFILE)

    def test_real_sender_policy_mismatch_makes_zero_marker_transport_calls(self) -> None:
        store, workflow, outbox = self.prepare_marker()
        # Restore the fictional, proven-unsent fixture marker for a real sender lease.
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE session_connect_outbox SET status='prepared',lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL WHERE outbox_id=?",
                (outbox.outbox_id,),
            )
        fixture = sender_fixtures.TelegramOutboxSenderTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        config = replace(fixture.config, state_path=self.path, codex_permission_profile=None)
        bot = sender_fixtures.Bot()
        sender = TelegramOutboxSender(
            config,
            telegram_bots={**{agent.agent_id: bot for agent in config.agents}, "hub": bot},
            sender_id="example-sender",
        )
        self.addCleanup(sender.close)
        self.assertTrue(sender._deliver_connect_one())
        self.assertEqual(bot.sent, [])
        self.assertEqual(store.get(workflow.workflow_id).stage, "failed")
        self.assertIsNone(self.state.active_session(self.topic.topic_id))

    def test_managed_followup_waits_for_full_preparation_instead_of_steering(self) -> None:
        self.activate()
        parent = self.enqueue()
        child = self.enqueue(message=2)
        leased = self.state.lease_provider_job("codex", "example-worker")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(parent.job_id, leased.lease_token)
        self.assertIsNone(self.state.lease_steer_followup(parent.job_id, "example-steerer"))
        self.assertEqual(self.state.get_provider_job(child.job_id).status, "queued")

    def alias_pair(self, *, retained_managed: bool = False):
        alias = "example-codex-alias"
        if retained_managed:
            # Defensive persisted-row coverage, not normal managed-alias admission.
            with self.state._connection:
                self.state._connection.execute(
                    "INSERT INTO agent_sessions (session_id,topic_id,agent_id,generation,status,"
                    "model,effort,created_at,updated_at,codex_permission_profile) "
                    "VALUES ('example-alias-session',?,?,1,'satellite','example-model','low',"
                    "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',?)",
                    (self.topic.topic_id, alias, PROFILE),
                )
        session = self.state.activate_agent(self.topic.topic_id, alias, "example-model", "low")
        jobs = [
            self.state.enqueue_provider_job(
                idempotency_key=f"example-alias:{message}",
                chat_id=self.topic.chat_id,
                message_id=message,
                topic_id=self.topic.topic_id,
                agent_id=alias,
                session_id=session.session_id,
                session_generation=session.generation,
                model=session.model,
                effort=session.effort,
                payload_text="Example task",
            )[0]
            for message in (1, 2)
        ]
        leased = self.state.lease_provider_job(alias, "example-worker")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(jobs[0].job_id, leased.lease_token)
        return self.state.get_provider_job(jobs[0].job_id), jobs[1]

    def test_codex_alias_steering_requires_explicit_unchanged_legacy_context(self) -> None:
        parent, child = self.alias_pair()
        for context in ({}, {"codex_permission_profile": PROFILE}):
            with (
                self.subTest(context=context),
                closing(HubState.open(self.path, **context)) as state,
            ):
                if not context:
                    with self.assertRaisesRegex(StateError, "configuration is unavailable"):
                        state.lease_steer_followup(parent.job_id, "example-steerer")
                else:
                    self.assertIsNone(state.lease_steer_followup(parent.job_id, "example-steerer"))
                self.assertEqual(state.get_provider_job(parent.job_id), parent)
                self.assertEqual(state.get_provider_job(child.job_id), child)
                self.assertFalse(state._connection.in_transaction)
        with closing(HubState.open(self.path, codex_permission_profile=None)) as state:
            accepted = state.lease_steer_followup(parent.job_id, "example-steerer")
            assert accepted is not None
            self.assertEqual(accepted.job_id, child.job_id)
            self.assertEqual(accepted.status, "leased")
            self.assertIsNone(accepted.codex_permission_profile)

    def test_retained_managed_codex_alias_keeps_followup_queued(self) -> None:
        parent, child = self.alias_pair(retained_managed=True)
        self.assertIsNone(self.state.lease_steer_followup(parent.job_id, "example-steerer"))
        self.assertEqual(self.state.get_provider_job(parent.job_id), parent)
        self.assertEqual(self.state.get_provider_job(child.job_id), child)

    def test_direct_writable_adoption_requires_explicit_context_before_mutation(self) -> None:
        request = AdoptionRequest(
            project_id="example-project",
            chat_id=self.topic.chat_id,
            thread_id=self.topic.thread_id,
            provider_thread_id="example-saved-thread",
            canonical_root=self.root,
            model="example-model",
            effort="low",
        )
        with open_adoption_state(self.path, writable=True) as unknown:
            with self.assertRaises(StateError):
                CodexSessionOrigins(unknown).attach(request, expected_session_id=None)
            self.assertIsNone(unknown.active_session(self.topic.topic_id))
            self.assertEqual(
                unknown._connection.execute(
                    "SELECT count(*) FROM codex_session_origins"
                ).fetchone()[0],
                0,
            )
        with open_adoption_state(self.path, writable=True, codex_permission_profile=None) as legacy:
            attached = CodexSessionOrigins(legacy).attach(request, expected_session_id=None)
            self.assertIsNone(attached.session.codex_permission_profile)

    def test_raw_snapshot_inserts_reject_missing_profile_but_allow_exact_legacy_null(self) -> None:
        self.activate()
        job = self.enqueue()
        with self.assertRaises(sqlite3.IntegrityError), self.state._connection:
            self.state._connection.execute(
                """INSERT INTO provider_jobs
                   (job_id,idempotency_key,chat_id,message_id,topic_id,topic_sequence,
                    agent_id,session_id,session_generation,model,effort,payload_text,
                    status,created_at,updated_at)
                   SELECT 'example-missing-job','example-missing-input',chat_id,2,topic_id,2,
                          agent_id,session_id,session_generation,model,effort,payload_text,
                          status,created_at,updated_at FROM provider_jobs WHERE job_id=?""",
                (job.job_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError), self.state._connection:
            self.state._connection.execute(
                "INSERT INTO provider_execution_checkpoints(job_id,provider_thread_id,project_root,updated_at) VALUES(?,?,?,'example-now')",
                (job.job_id, "example-thread", str(self.root)),
            )
        with closing(
            HubState.open(self.root / "legacy.db", codex_permission_profile=None)
        ) as legacy:
            topic = legacy.observe_topic(
                project_id="example-project",
                chat_id=self.topic.chat_id,
                thread_id=8,
                title="Example legacy",
            )
            session = legacy.activate_agent(topic.topic_id, "codex", "example-model", "low")
            legacy_job, _ = legacy.enqueue_provider_job(
                idempotency_key="example-legacy",
                chat_id=topic.chat_id,
                message_id=1,
                topic_id=topic.topic_id,
                agent_id="codex",
                session_id=session.session_id,
                session_generation=session.generation,
                model=session.model,
                effort=session.effort,
                payload_text="Example legacy request",
            )
            with legacy._connection:
                legacy._connection.execute(
                    "INSERT INTO provider_execution_checkpoints(job_id,provider_thread_id,project_root,updated_at) VALUES(?,?,?,'example-now')",
                    (legacy_job.job_id, "example-thread", str(self.root)),
                )
            checkpoint = ExecutionJournal(legacy).read(legacy_job.job_id)
            assert checkpoint is not None
            self.assertIsNone(checkpoint["codex_permission_profile"])

    def test_continuation_refuses_missing_changed_context_and_copies_exact_selection(self) -> None:
        self.activate()
        job = self.enqueue()
        lease = self.state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, lease.lease_token)
        journal = ExecutionJournal(self.state)
        journal.record_thread(
            job.job_id,
            lease.lease_token,
            "example-thread",
            self.root,
            codex_permission_profile=PROFILE,
        )
        journal.record_turn(job.job_id, lease.lease_token, "example-turn")
        self.state.terminate_provider_job_with_notice(
            job.job_id,
            lease.lease_token,
            status="indeterminate",
            expected_status="executing",
            error_class="runtime",
            error_code="example_failure",
            sender_agent_id="codex",
            telegram_html="Example failure notice",
        )
        with self.state._connection:
            self.state._connection.execute(
                "INSERT INTO provider_turn_terminal_evidence VALUES(?,?,?,?,?,?)",
                (
                    job.job_id,
                    "failed",
                    "example-thread",
                    "example-turn",
                    str(self.root),
                    "example-now",
                ),
            )
        delivery = self.state.lease_telegram_outbox("codex", "example-sender")
        assert delivery is not None and delivery.lease_token is not None
        self.state.mark_telegram_outbox_delivered(
            delivery.outbox_id, delivery.lease_token, telegram_message_id=101
        )
        arguments: dict[str, Any] = dict(
            source_job_id=job.job_id,
            chat_id=self.topic.chat_id,
            thread_id=self.topic.thread_id,
            notice_message_id=101,
            reply_message_id=102,
            canonical_root=self.root,
        )
        for context in (
            {},
            {"codex_permission_profile": None},
            {"codex_permission_profile": "example-other-policy"},
        ):
            with (
                self.subTest(context=context),
                closing(HubState.open(self.path, **context)) as changed,
            ):
                with self.assertRaises(StateError):
                    TurnContinuationState(changed).continue_from_notice(**arguments)
                self.assertEqual(len(changed.provider_jobs_for_topic(self.topic.topic_id)), 1)
        continued, created, _ = TurnContinuationState(self.state).continue_from_notice(**arguments)
        self.assertTrue(created)
        self.assertEqual(continued.codex_permission_profile, PROFILE)
        self.assertEqual(continued.provider_session_id, "example-thread")


if __name__ == "__main__":
    unittest.main()
