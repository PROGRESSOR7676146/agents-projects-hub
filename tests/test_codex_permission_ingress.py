from __future__ import annotations

import hashlib
import unittest
from contextlib import closing
from dataclasses import replace
from typing import Any, cast
from unittest.mock import Mock, patch

from hermes_codex_router.codex_appserver import CodexTurnError, RpcError, StoredTurnOutcome
from hermes_codex_router.hub_config import AgentDefinition
from hermes_codex_router.outbox_sender import TelegramOutboxSender
from hermes_codex_router.service import QueueAcceptanceError
from hermes_codex_router.state import HubState
from tests import test_embedded_queue_service as fixtures
from tests.test_service_integration import callback, callback_values

PROFILE = "example-project-policy"


class ManagedPermissionIngressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.EmbeddedQueueServiceTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.client = fixtures.QueueClient()
        self.service, self.telegram = self.fixture.service(self.client)
        self.addCleanup(lambda: self.service.state.close())
        self.project = self.service.registry.projects[0]
        self.topic = self.service.state.observe_topic(
            project_id=self.project.project_id,
            chat_id=-1001234567890,
            thread_id=77,
            title="Example",
            execution_root=self.project.root,
        )
        self.session = self.service.state.activate_agent(
            self.topic.topic_id, "codex", "example-old-model", "low"
        )

    def change_profile(self, profile: str | None = PROFILE) -> None:
        self.service.state.close()
        self.service.config = replace(
            self.service.config,
            codex_permission_profile=profile,
            queue_runtime="external",
            outbox_runtime="external",
        )
        self.service.state = HubState.open(
            self.service.config.state_path,
            codex_permission_profile=profile,
        )

    def assert_refusal(self) -> None:
        rows = self.service.state._connection.execute("SELECT * FROM hub_blocker_outbox").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["event_key"].startswith("codex_permission_selection_changed:"))
        self.assertIn("/new", rows[0]["telegram_html"])
        for column in (
            "blocker_topic_id",
            "blocker_session_id",
            "blocker_generation",
            "blocker_kind",
            "blocker_job_id",
            "job_id",
            "reply_markup_json",
        ):
            self.assertIsNone(rows[0][column], column)
        self.assertEqual(self.service.state.provider_jobs_for_topic(self.topic.topic_id), ())
        self.assertEqual(self.client.started_threads, 0)

    def confirm_new(self, message_id: int) -> None:
        with patch.object(self.telegram, "send_html", wraps=self.telegram.send_html) as send:
            self.assertTrue(self.service.handle_update(fixtures.update(message_id, "/new")))
        confirmation = next(
            value
            for value in callback_values(send.call_args.kwargs.get("reply_markup"))
            if value.startswith("new:confirm:")
        )
        self.assertTrue(
            self.service.handle_update(callback(message_id + 1, "example-new", confirmation))
        )

    def test_stale_active_recovers_through_owner_new_callback(self) -> None:
        self.change_profile()
        self.assertTrue(self.service.handle_update(fixtures.update(1, "Old example task")))
        self.assertTrue(self.service.handle_update(fixtures.update(2, "/agent codex")))
        self.confirm_new(3)
        active = self.service.state.active_session(self.topic.topic_id)
        assert active is not None
        self.assertNotEqual(active.session_id, self.session.session_id)
        self.assertEqual(active.codex_permission_profile, PROFILE)
        self.assertTrue(self.service.handle_update(fixtures.update(5, "Current example task")))
        jobs = self.service.state.provider_jobs_for_topic(self.topic.topic_id)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            (jobs[0].session_id, jobs[0].codex_permission_profile), (active.session_id, PROFILE)
        )
        self.assertEqual(self.client.started_threads, 0)

    def test_stale_active_input_is_durable_and_duplicate_after_restart_and_new(self) -> None:
        self.change_profile()
        self.assertTrue(self.service.handle_update(fixtures.update(1, "Example task")))
        self.assert_refusal()
        self.change_profile()
        self.assertFalse(self.service.handle_update(fixtures.update(1, "Example task")))
        self.service.state.new_active_session(self.topic.topic_id)
        self.assertFalse(self.service.handle_update(fixtures.update(1, "Example task")))
        self.assert_refusal()
        self.assertTrue(self.service.handle_update(fixtures.update(2, "New example task")))
        jobs = self.service.state.provider_jobs_for_topic(self.topic.topic_id)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].codex_permission_profile, PROFILE)

    def test_stale_satellite_refuses_before_material_download_then_explicit_switch_and_new(
        self,
    ) -> None:
        claude = AgentDefinition(
            "claude",
            "Claude",
            "example_claude_bot",
            "claude",
            None,
            True,
            False,
            "example-model",
            "default",
        )
        self.service.config = replace(
            self.service.config, agents=(*self.service.config.agents, claude)
        )
        self.service.usernames["claude"] = claude.telegram_username
        self.service.state.activate_agent(
            self.topic.topic_id, "claude", claude.default_model, claude.default_effort
        )
        self.change_profile()
        incoming = fixtures.update(1, "@example_codex_bot Example attachment")
        message = incoming["message"]
        assert isinstance(message, dict)
        message["document"] = {
            "file_id": "example-file",
            "file_unique_id": "example-unique",
            "file_name": "example.txt",
            "mime_type": "text/plain",
            "file_size": 4,
        }
        with patch(
            "hermes_codex_router.controller_admission.receive_incoming_materials"
        ) as download:
            self.assertTrue(self.service.handle_update(incoming))
        download.assert_not_called()
        self.assert_refusal()
        self.assertTrue(self.service.handle_update(fixtures.update(2, "/agent codex")))
        active = self.service.state.active_session(self.topic.topic_id)
        assert active is not None
        self.assertEqual(
            (active.session_id, active.model, active.effort, active.codex_permission_profile),
            (self.session.session_id, self.session.model, self.session.effort, None),
        )
        self.assertIn("/new", self.telegram.sent[-1])
        self.confirm_new(3)
        new = self.service.state.active_session(self.topic.topic_id)
        assert new is not None
        self.assertEqual(new.codex_permission_profile, PROFILE)
        self.assertTrue(self.service.handle_update(fixtures.update(5, "Current example task")))
        jobs = self.service.state.provider_jobs_for_topic(self.topic.topic_id)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(
            (jobs[0].session_id, jobs[0].codex_permission_profile), (new.session_id, PROFILE)
        )

    def test_refusal_transaction_rolls_back_receipt_and_notice_together(self) -> None:
        self.change_profile()
        with self.service.state._connection:
            self.service.state._connection.execute(
                "CREATE TRIGGER example_receipt_failure BEFORE INSERT ON observed_messages BEGIN SELECT RAISE(ABORT,'example failure'); END"
            )
        with self.assertRaises(QueueAcceptanceError):
            self.service.handle_update(fixtures.update(1, "Example task"))
        self.assertFalse(self.service.state.message_already_observed(self.topic.chat_id, 1))
        self.assertEqual(
            self.service.state._connection.execute(
                "SELECT count(*) FROM hub_blocker_outbox"
            ).fetchone()[0],
            0,
        )
        with self.service.state._connection:
            self.service.state._connection.execute("DROP TRIGGER example_receipt_failure")
        self.assertTrue(self.service.handle_update(fixtures.update(1, "Example task")))
        self.assert_refusal()

    def test_session_replacement_during_service_admission_retains_input_for_redelivery(
        self,
    ) -> None:
        self.change_profile(None)
        original = self.service._enqueue_provider_turn

        def replace_before_admission(**kwargs):
            self.service.state.new_active_session(self.topic.topic_id)
            return original(**kwargs)

        with patch.object(
            self.service, "_enqueue_provider_turn", side_effect=replace_before_admission
        ):
            with self.assertRaises(QueueAcceptanceError):
                self.service.handle_update(fixtures.update(1, "Example task"))
        self.assertFalse(self.service.state.message_already_observed(self.topic.chat_id, 1))
        self.assertEqual(self.service.state.provider_jobs_for_topic(self.topic.topic_id), ())
        self.assertTrue(self.service.handle_update(fixtures.update(1, "Example task")))
        self.assertEqual(len(self.service.state.provider_jobs_for_topic(self.topic.topic_id)), 1)

    def test_legacy_local_return_under_managed_config_changes_only_ownership(self) -> None:
        self.service.state.bind_provider_session(self.session.session_id, "example-thread", None)
        self.service.state.set_writer_mode(self.session.session_id, "local")
        self.change_profile()
        self.assertTrue(self.service.handle_update(fixtures.update(1, "/return")))
        returned = self.service.state.get_session(self.session.session_id)
        self.assertEqual(
            (returned.writer_mode, returned.provider_session_id, returned.codex_permission_profile),
            ("telegram", "example-thread", None),
        )
        self.assertEqual(self.client.started_threads, 0)
        self.assertEqual(self.service.state.provider_jobs_for_topic(self.topic.topic_id), ())
        self.assertIn("/new", self.telegram.sent[-1])

    def test_local_satellite_can_be_promoted_for_ownership_return(self) -> None:
        claude = AgentDefinition(
            "claude",
            "Claude",
            "example_claude_bot",
            "claude",
            None,
            True,
            False,
            "example-model",
            "default",
        )
        self.service.config = replace(
            self.service.config, agents=(*self.service.config.agents, claude)
        )
        self.service.usernames["claude"] = claude.telegram_username
        self.service.state.bind_provider_session(self.session.session_id, "example-thread", None)
        self.service.state.set_writer_mode(self.session.session_id, "local")
        # Historical state could demote a local writer without a control snapshot.
        self.service.state.activate_agent(self.topic.topic_id, "claude", "example-model", "default")
        self.change_profile()
        self.assertTrue(self.service.handle_update(fixtures.update(1, "/agent codex")))
        active = self.service.state.active_session(self.topic.topic_id)
        assert active is not None
        self.assertEqual(
            (active.session_id, active.writer_mode), (self.session.session_id, "local")
        )
        self.assertIn("/return", self.telegram.sent[-1])
        self.assertTrue(self.service.handle_update(fixtures.update(2, "/new")))
        self.assertEqual(self.service.state.get_session(self.session.session_id).status, "active")
        self.assertTrue(self.service.handle_update(fixtures.update(3, "/return")))
        active = self.service.state.get_session(self.session.session_id)
        self.assertEqual(
            (active.writer_mode, active.provider_session_id, active.codex_permission_profile),
            ("telegram", "example-thread", None),
        )
        self.assertEqual(self.client.started_threads, 0)
        self.assertEqual(self.service.state.provider_jobs_for_topic(self.topic.topic_id), ())

    def test_changed_profile_reply_retry_has_one_durable_new_hint_and_no_job(self) -> None:
        class Client(fixtures.QueueClient):
            def wait_for_turn(self, _turn_id: str):
                raise CodexTurnError(RpcError("fictional transport failure"))

            def read_turn_outcome(self, **_kwargs: object) -> StoredTurnOutcome:
                return StoredTurnOutcome("failed")

        cast(Any, self.service.supervisor).client_value = Client()
        self.assertTrue(self.service.handle_update(fixtures.update(1, "Original example task")))
        self.assertTrue(self.service.run_embedded_queue_cycle())
        old = self.service.state.provider_jobs_for_topic(self.topic.topic_id)[0]
        notice = self.service.state.get_telegram_outbox_for_job(old.job_id)
        assert notice.telegram_message_id is not None
        self.change_profile()
        reply = fixtures.update(2, "retry")
        cast(dict[str, Any], reply["message"])["reply_to_message"] = {
            "message_id": notice.telegram_message_id,
            "from": {"is_bot": True, "username": "example_codex_bot"},
        }
        with self.service.state._connection:
            self.service.state._connection.execute(
                "CREATE TRIGGER example_retry_receipt_failure BEFORE INSERT ON observed_messages "
                "BEGIN SELECT RAISE(ABORT,'example retry failure'); END"
            )
        with self.assertRaises(QueueAcceptanceError):
            self.service.handle_update(reply)
        self.assertFalse(self.service.state.message_already_observed(self.topic.chat_id, 2))
        self.assertEqual(
            self.service.state._connection.execute(
                "SELECT count(*) FROM hub_blocker_outbox"
            ).fetchone()[0],
            0,
        )
        with self.service.state._connection:
            self.service.state._connection.execute("DROP TRIGGER example_retry_receipt_failure")
        self.assertTrue(self.service.handle_update(reply))
        rows = self.service.state._connection.execute("SELECT * FROM hub_blocker_outbox").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("/new", rows[0]["telegram_html"])
        self.assertTrue(self.service.state.message_already_observed(self.topic.chat_id, 2))
        self.assertFalse(self.service.handle_update(reply))
        self.assertEqual(len(self.service.state.provider_jobs_for_topic(self.topic.topic_id)), 1)
        self.assertIsNone(
            self.service.state._connection.execute(
                "SELECT 1 FROM provider_job_continuations"
            ).fetchone()
        )

    def test_terminal_satellite_release_preserves_binding_and_failed_release_keeps_writer(
        self,
    ) -> None:
        claude = AgentDefinition(
            "claude",
            "Claude",
            "example_claude_bot",
            "claude",
            None,
            True,
            False,
            "example-model",
            "default",
        )
        self.service.config = replace(
            self.service.config, agents=(*self.service.config.agents, claude)
        )
        self.service.usernames["claude"] = claude.telegram_username
        self.service.state.bind_provider_session(
            self.session.session_id, "example-thread", "example-terminal"
        )
        self.service.state.set_writer_mode(self.session.session_id, "terminal")
        self.service.state.activate_agent(self.topic.topic_id, "claude", "example-model", "default")
        self.change_profile()
        terminal = Mock()
        self.service.terminal = cast(Any, terminal)
        self.assertTrue(self.service.handle_update(fixtures.update(1, "/agent codex")))
        self.assertIn("/release", self.telegram.sent[-1])
        active = self.service.state.active_session(self.topic.topic_id)
        assert active is not None
        self.assertEqual(
            (active.session_id, active.writer_mode), (self.session.session_id, "terminal")
        )
        self.assertTrue(self.service.handle_update(fixtures.update(2, "/new")))
        self.assertEqual(self.service.state.get_session(self.session.session_id).status, "active")
        terminal.release.side_effect = RuntimeError("example unconfirmed release")
        with self.assertRaises(RuntimeError):
            self.service.handle_update(fixtures.update(3, "/release"))
        self.assertEqual(
            self.service.state.get_session(self.session.session_id).writer_mode, "terminal"
        )
        terminal.release.side_effect = None
        self.assertTrue(self.service.handle_update(fixtures.update(4, "/release")))
        returned = self.service.state.get_session(self.session.session_id)
        self.assertEqual(
            (returned.writer_mode, returned.provider_session_id, returned.codex_permission_profile),
            ("telegram", "example-thread", None),
        )
        terminal.start.assert_not_called()
        self.assertEqual(self.client.started_threads, 0)
        self.assertEqual(self.service.state.provider_jobs_for_topic(self.topic.topic_id), ())

    def test_input_notice_survives_release_and_both_actual_senders(self) -> None:
        for standalone in (False, True):
            with self.subTest(standalone=standalone):
                message_id = 10 + int(standalone)
                self.change_profile()
                self.assertTrue(
                    self.service.handle_update(fixtures.update(message_id, "Example task"))
                )
                with self.service.state._immediate_transaction():
                    self.service.state._root_blocker_state.notice_released_scope(
                        session_id=self.session.session_id, generation=self.session.generation
                    )
                before = len(self.telegram.sent)
                if standalone:
                    sender = TelegramOutboxSender(
                        self.service.config,
                        telegram_bots=cast(Any, {"codex": self.telegram, "hub": self.telegram}),
                    )
                    try:
                        self.assertTrue(sender._deliver_root_blocker_one())
                        self.assertFalse(sender._deliver_root_blocker_one())
                    finally:
                        sender.close()
                else:
                    self.assertTrue(
                        self.service._deliver_embedded_blocker_notice(self.service.state)
                    )
                    self.assertFalse(
                        self.service._deliver_embedded_blocker_notice(self.service.state)
                    )
                self.assertEqual(len(self.telegram.sent), before + 1)
                row = self.service.state._connection.execute(
                    "SELECT * FROM hub_blocker_outbox WHERE reply_to_message_id=?", (message_id,)
                ).fetchone()
                self.assertEqual((row["status"], row["attempt_count"]), ("delivered", 1))
                self.assertIsNone(row["reply_markup_json"])

    def album(self, agent_id: str, *, active_agent_id: str):
        claude = AgentDefinition(
            "claude",
            "Claude",
            "example_claude_bot",
            "claude",
            None,
            True,
            False,
            "example-model",
            "default",
        )
        self.service.config = replace(
            self.service.config,
            agents=(*self.service.config.agents, claude),
            external_worker_agent_ids=("codex", "claude"),
        )
        self.service.usernames["claude"] = claude.telegram_username
        target = (
            self.session
            if agent_id == "codex"
            else self.service.state.ensure_satellite(
                self.topic.topic_id, "claude", "example-model", "default"
            )
        )
        raw_group = f"{self.topic.chat_id}:{self.topic.thread_id}:example-album".encode()
        job, _ = self.service.state.enqueue_provider_job(
            idempotency_key="example-album",
            chat_id=self.topic.chat_id,
            message_id=1,
            topic_id=self.topic.topic_id,
            agent_id=agent_id,
            session_id=target.session_id,
            session_generation=target.generation,
            model=target.model,
            effort=target.effort,
            payload_text="Example first part",
            input_group_key="telegram-album:" + hashlib.sha256(raw_group).hexdigest(),
        )
        if active_agent_id == "claude":
            self.service.state.activate_agent(
                self.topic.topic_id, "claude", "example-model", "default"
            )
        self.change_profile()
        incoming = fixtures.update(2, "Example second part")
        message = incoming["message"]
        assert isinstance(message, dict)
        message["media_group_id"] = "example-album"
        return job, incoming

    def test_inherited_stale_codex_album_refuses_without_extending_old_hold(self) -> None:
        job, incoming = self.album("codex", active_agent_id="claude")
        before = self.service.state.get_provider_job(job.job_id)
        self.assertTrue(self.service.handle_update(incoming))
        self.assertEqual(self.service.state.get_provider_job(job.job_id), before)
        self.assertEqual(len(self.service.state.provider_jobs_for_topic(self.topic.topic_id)), 1)
        notice = self.service.state._connection.execute(
            "SELECT event_key FROM hub_blocker_outbox"
        ).fetchone()
        self.assertTrue(notice["event_key"].startswith("codex_permission_selection_changed:"))

    def test_valid_claude_album_does_not_use_provisional_stale_codex_refusal(self) -> None:
        job, incoming = self.album("claude", active_agent_id="codex")
        self.assertTrue(self.service.handle_update(incoming))
        self.assertEqual(
            self.service.state._connection.execute(
                "SELECT count(*) FROM hub_blocker_outbox"
            ).fetchone()[0],
            0,
        )
        jobs = self.service.state.provider_jobs_for_topic(self.topic.topic_id)
        self.assertTrue(all(item.agent_id == "claude" for item in jobs))
        self.assertTrue(any("second part" in item.payload_text for item in jobs))

    def test_satellite_created_by_other_profile_after_early_gate_keeps_input_retryable(
        self,
    ) -> None:
        claude = AgentDefinition(
            "claude",
            "Claude",
            "example_claude_bot",
            "claude",
            None,
            True,
            False,
            "example-model",
            "default",
        )
        self.service.config = replace(
            self.service.config, agents=(*self.service.config.agents, claude)
        )
        self.service.usernames["claude"] = claude.telegram_username
        self.service.state.activate_agent(self.topic.topic_id, "claude", "example-model", "default")
        with self.service.state._connection:
            self.service.state._connection.execute(
                "UPDATE agent_sessions SET status='archived' WHERE session_id=?",
                (self.session.session_id,),
            )
        self.change_profile()
        original = self.service.state.ensure_satellite

        def create_from_other_config(*args, **kwargs):
            with closing(
                HubState.open(
                    self.service.config.state_path, codex_permission_profile="example-other-policy"
                )
            ) as other:
                other.ensure_satellite(self.topic.topic_id, "codex", "example-model", "low")
            return original(*args, **kwargs)

        incoming = fixtures.update(1, "@example_codex_bot Example task")
        with patch.object(
            self.service.state, "ensure_satellite", side_effect=create_from_other_config
        ):
            with self.assertRaises(QueueAcceptanceError):
                self.service.handle_update(incoming)
        self.assertFalse(self.service.state.message_already_observed(self.topic.chat_id, 1))
        self.assertEqual(self.service.state.provider_jobs_for_topic(self.topic.topic_id), ())
        self.assertTrue(self.service.handle_update(incoming))
        self.assert_refusal()


if __name__ == "__main__":
    unittest.main()
