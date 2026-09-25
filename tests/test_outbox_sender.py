from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.cli import main
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    HubTelegramBot,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.outbox_sender import TelegramOutboxSender
from hermes_codex_router.project_onboarding import ProjectOnboardingStore
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.state import HubState
from hermes_codex_router.telegram import TelegramError


class Bot:
    def __init__(
        self,
        *,
        fail: bool = False,
        command_errors: dict[str, BaseException] | None = None,
    ) -> None:
        self.fail = fail
        self.command_errors = command_errors or {}
        self.sent: list[tuple[int, int, str]] = []
        self.documents: list[tuple[int, int, Path, str | None]] = []
        self.actions: list[tuple[int, int, str]] = []
        self.drafts: list[tuple[int, int, int, str]] = []
        self.commands: dict[str | None, list[dict[str, str]]] = {}

    def call(self, method: str, **params: object) -> object:
        scope = cast(str | None, params.get("scope"))
        error = self.command_errors.get(scope or "")
        if error is not None:
            raise error
        if method == "setMyCommands":
            self.commands[scope] = cast(list[dict[str, str]], json.loads(str(params["commands"])))
            return True
        if method == "getMyCommands":
            return self.commands.get(scope, [])
        raise AssertionError(method)

    def send_chat_action(self, chat_id: int, thread_id: int, action: str = "typing") -> None:
        self.actions.append((chat_id, thread_id, action))

    def send_html(
        self,
        chat_id: int,
        thread_id: int,
        html: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        self.sent.append((chat_id, thread_id, html))
        if self.fail:
            raise RuntimeError("transport unavailable")
        return len(self.sent) + len(self.documents)

    def send_document(
        self,
        chat_id: int,
        thread_id: int,
        document_path: Path,
        *,
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> int:
        self.documents.append((chat_id, thread_id, document_path, caption))
        if self.fail:
            raise RuntimeError("transport unavailable")
        return len(self.sent) + len(self.documents)

    def send_message_draft(
        self, chat_id: int, thread_id: int, *, draft_id: int, text: str = ""
    ) -> None:
        self.drafts.append((chat_id, thread_id, draft_id, text))


class TransportFailBot(Bot):
    def send_html(
        self,
        chat_id: int,
        thread_id: int,
        html: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
    ) -> int:
        self.sent.append((chat_id, thread_id, html))
        if self.fail:
            raise TelegramError(
                "safe Telegram failure",
                operation="send_message",
                failure_class="network_timeout",
            )
        return len(self.sent) + len(self.documents)


class TelegramOutboxSenderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.base = base
        self.dynamic_projects: list[dict[str, object]] = []
        self.config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=base / "projects.json",
            state_path=base / "state.db",
            codex_socket_path=base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(ProjectBinding("example-project", -1001234567890),),
            agents=(
                AgentDefinition(
                    "opencode",
                    "OpenCode",
                    "example_open_bot",
                    "opencode",
                    None,
                    False,
                    False,
                    "provider-selected",
                    "high",
                ),
                AgentDefinition(
                    "antigravity",
                    "Antigravity",
                    "example_agy_bot",
                    "antigravity",
                    None,
                    False,
                    False,
                    "provider-selected",
                    "high",
                ),
            ),
            dispatch_mode="queue",
            queue_runtime="external",
            outbox_runtime="external",
            external_worker_agent_ids=("opencode", "antigravity"),
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_sender_retries_only_blocker_notice_after_transport_failure(self) -> None:
        token = self.base / "hub.token"
        token.write_text("fictional-token", encoding="utf-8")
        token.chmod(0o600)
        config = replace(self.config, hub_bot=HubTelegramBot("example_hub_bot", token))
        state = HubState.open(config.state_path)
        try:
            owner = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=191,
                title="Fictional owner",
            )
            destination = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=192,
                title="Fictional destination",
            )
            local = state.activate_agent(owner.topic_id, "opencode", "model-1", "high")
            selected = state.activate_agent(destination.topic_id, "opencode", "model-1", "high")
            state.set_writer_mode(local.session_id, "local")
            notice = state.reject_blocked_provider_input(
                chat_id=destination.chat_id,
                message_id=1901,
                topic_id=destination.topic_id,
                session_id=selected.session_id,
                session_generation=selected.generation,
            )
            assert notice is not None
        finally:
            state.close()
        hub = Bot(fail=True)
        sender = TelegramOutboxSender(
            config,
            telegram_bots=cast(
                dict[str, Any],
                {
                    "hub": hub,
                    "opencode": Bot(),
                    "antigravity": Bot(),
                },
            ),
        )
        try:
            for _ in range(6):
                self.assertTrue(
                    sender.run_cycle(now=datetime.now(timezone.utc) + timedelta(hours=1))
                )
            self.assertEqual(len(hub.sent), 6)
            self.assertEqual(sender.state.provider_jobs_for_topic(destination.topic_id), ())
            pending = sender.state._connection.execute(
                "SELECT status,attempt_count FROM hub_blocker_outbox WHERE outbox_id=?",
                (notice.outbox_id,),
            ).fetchone()
            self.assertEqual(tuple(pending), ("pending", 6))
            hub.fail = False
            self.assertTrue(sender.run_cycle(now=datetime.now(timezone.utc) + timedelta(hours=1)))
            self.assertEqual(len(hub.sent), 7)
            delivered = sender.state._connection.execute(
                "SELECT status,attempt_count FROM hub_blocker_outbox WHERE outbox_id=?",
                (notice.outbox_id,),
            ).fetchone()
            self.assertEqual(tuple(delivered), ("delivered", 7))
        finally:
            sender.close()

    def ready_outbox(
        self, agent_id: str, message_id: int, *, telegram_html: str | None = None
    ) -> str:
        state = HubState.open(self.config.state_path)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=70 + message_id,
                title="Example",
            )
            session = state.activate_agent(topic.topic_id, agent_id, "model-1", "high")
            job, _ = state.enqueue_provider_job(
                idempotency_key=f"telegram:-1001234567890:{message_id}",
                chat_id=-1001234567890,
                message_id=message_id,
                topic_id=topic.topic_id,
                agent_id=agent_id,
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=None,
                model=session.model,
                effort=session.effort,
                payload_text="durable task",
                context_watermark=None,
                handoff_id=None,
            )
            leased = state.lease_provider_job(agent_id, "test-worker")
            assert leased is not None and leased.lease_token is not None
            state.mark_provider_job_executing(job.job_id, leased.lease_token)
            state.commit_provider_result(
                job.job_id,
                leased.lease_token,
                visible_response=telegram_html or f"{agent_id} result",
                sender_agent_id=agent_id,
                telegram_html=telegram_html or f"<b>{agent_id} result</b>",
            )
            return job.job_id
        finally:
            state.close()

    def sender(self, **bots: Bot) -> TelegramOutboxSender:
        return TelegramOutboxSender(
            self.config,
            telegram_bots=cast(dict[str, Any], bots),
            sender_id="test-sender",
        )

    def add_dynamic_binding(self, project_id: str, chat_id: int) -> str:
        root = self.base / project_id
        root.mkdir()
        subprocess.run(("git", "init", "-q", str(root)), check=True)
        self.dynamic_projects.append(
            {
                "project_id": project_id,
                "display_name": project_id.title(),
                "topic_name": project_id.title(),
                "root": str(root),
            }
        )
        self.config.registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_roots": [str(self.base)],
                    "projects": self.dynamic_projects,
                }
            ),
            encoding="utf-8",
        )
        state = HubState.open(self.config.state_path)
        try:
            now = datetime.now(timezone.utc)
            workflow_id = f"workflow-{project_id}"
            state._connection.execute(
                """INSERT INTO project_onboarding_workflows
                   (workflow_id,owner_user_id,display_name,project_id,base_root,canonical_root,
                    stage,expires_at,created_at,updated_at,required_owner_ids_json)
                   VALUES (?,?,?,?,?,?,'completed',?,?,?,'[42]')""",
                (
                    workflow_id,
                    42,
                    project_id.title(),
                    project_id,
                    str(self.base),
                    str(root),
                    (now + timedelta(hours=1)).isoformat(),
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            state._connection.execute(
                """INSERT INTO project_group_bindings
                   (project_id,telegram_chat_id,canonical_root,workflow_id,created_at)
                   VALUES (?,?,?,?,?)""",
                (project_id, chat_id, str(root), workflow_id, now.isoformat()),
            )
            state._connection.commit()
            return workflow_id
        finally:
            state.close()

    def test_final_result_precedes_new_project_command_scope(self) -> None:
        chat_id = -1002222222222
        self.add_dynamic_binding("dynamic", chat_id)
        job_id = self.ready_outbox("opencode", 901)
        opencode = Bot()
        antigravity = Bot()
        sender = self.sender(opencode=opencode, antigravity=antigravity)
        try:
            self.assertTrue(sender.run_cycle())
            self.assertEqual(sender.state.get_provider_job(job_id).status, "completed")
            self.assertEqual(opencode.commands, {})
            for _ in range(4):
                self.assertTrue(sender.run_cycle())
            rows = sender.state._connection.execute(
                "SELECT status FROM project_command_scopes WHERE telegram_chat_id=?", (chat_id,)
            ).fetchall()
            self.assertEqual([row["status"] for row in rows], ["ready", "ready"])
        finally:
            sender.close()

    def test_broken_project_command_scope_does_not_block_another_group(self) -> None:
        broken_chat = -1002222222222
        healthy_chat = -1001111111111
        self.add_dynamic_binding("broken", broken_chat)
        self.add_dynamic_binding("healthy", healthy_chat)
        broken_scope = f'{{"type":"chat","chat_id":{broken_chat}}}'
        sender = self.sender(
            opencode=Bot(command_errors={broken_scope: RuntimeError("scope failure")}),
            antigravity=Bot(),
        )
        try:
            for _ in range(10):
                sender.run_cycle()
            rows = {
                (int(row["telegram_chat_id"]), str(row["bot_identity"])): str(row["status"])
                for row in sender.state._connection.execute(
                    "SELECT telegram_chat_id,bot_identity,status FROM project_command_scopes"
                )
            }
            self.assertEqual(rows[(broken_chat, "opencode")], "pending")
            self.assertEqual(rows[(healthy_chat, "antigravity")], "ready")
            self.assertEqual(rows[(healthy_chat, "opencode")], "ready")
        finally:
            sender.close()

    def test_project_command_429_cooldown_survives_sender_restart(self) -> None:
        chat_id = -1002222222222
        self.add_dynamic_binding("dynamic", chat_id)
        scope = f'{{"type":"chat","chat_id":{chat_id}}}'
        rate_limit = TelegramError(
            "rate limited",
            operation="api_call",
            failure_class="api_rejection",
            status_code=429,
            retry_after=60,
        )
        config = replace(
            self.config,
            agents=(self.config.require_agent("antigravity"),),
            external_worker_agent_ids=("antigravity",),
        )
        sender = TelegramOutboxSender(
            config,
            telegram_bots=cast(Any, {"antigravity": Bot(command_errors={scope: rate_limit})}),
            sender_id="test-sender",
        )
        try:
            self.assertTrue(sender.run_cycle())
            row = sender.state._connection.execute(
                """SELECT status,attempt_count,available_at FROM project_command_scopes
                   WHERE telegram_chat_id=? AND bot_identity='antigravity'""",
                (chat_id,),
            ).fetchone()
            self.assertEqual((row["status"], row["attempt_count"]), ("pending", 1))
            self.assertGreater(
                datetime.fromisoformat(str(row["available_at"])), datetime.now(timezone.utc)
            )
        finally:
            sender.close()

        second_chat = -1001111111111
        self.add_dynamic_binding("second", second_chat)
        restarted = TelegramOutboxSender(
            config,
            telegram_bots=cast(Any, {"antigravity": Bot()}),
            sender_id="test-sender-2",
        )
        try:
            self.assertFalse(restarted.run_cycle())
            rows = restarted.state._connection.execute(
                """SELECT telegram_chat_id,attempt_count,available_at
                   FROM project_command_scopes WHERE bot_identity='antigravity'"""
            ).fetchall()
            self.assertEqual({int(row["telegram_chat_id"]) for row in rows}, {chat_id, second_chat})
            self.assertTrue(all(int(row["attempt_count"]) <= 1 for row in rows))
            self.assertTrue(
                all(
                    datetime.fromisoformat(str(row["available_at"])) > datetime.now(timezone.utc)
                    for row in rows
                )
            )
            restarted.state._connection.execute(
                "UPDATE project_command_cooldowns SET available_at='2020-01-01T00:00:00+00:00'"
            )
            restarted.state._connection.execute(
                "UPDATE project_command_scopes SET available_at='2020-01-01T00:00:00+00:00'"
            )
            restarted.state._connection.commit()
            self.assertTrue(restarted.run_cycle())
            self.assertTrue(restarted.run_cycle())
        finally:
            restarted.close()

    def test_exhausted_stale_command_task_fails_without_blocking_another(self) -> None:
        exhausted_chat = -1002222222222
        healthy_chat = -1001111111111
        self.add_dynamic_binding("exhausted", exhausted_chat)
        self.add_dynamic_binding("healthy", healthy_chat)
        state = HubState.open(self.config.state_path)
        try:
            store = ProjectOnboardingStore(state)
            store.ensure_command_scope_tasks(("opencode",))
            state._connection.execute(
                """UPDATE project_command_scopes SET status='leased',attempt_count=20,
                   lease_token='expired-token',lease_owner='dead',
                   lease_expires_at='2020-01-01T00:00:00+00:00'
                   WHERE telegram_chat_id=? AND bot_identity='opencode'""",
                (exhausted_chat,),
            )
            state._connection.commit()
            task = store.claim_command_scope("replacement", ("opencode",))
            assert task is not None
            self.assertEqual(task.telegram_chat_id, healthy_chat)
            exhausted = state._connection.execute(
                """SELECT status,attempt_count FROM project_command_scopes
                   WHERE telegram_chat_id=? AND bot_identity='opencode'""",
                (exhausted_chat,),
            ).fetchone()
            self.assertEqual((exhausted["status"], exhausted["attempt_count"]), ("failed", 20))
            store.release_command_scope(task)
            store.reset_failed_command_scope(exhausted_chat, "opencode")
            reset = state._connection.execute(
                """SELECT status,phase,attempt_count FROM project_command_scopes
                   WHERE telegram_chat_id=? AND bot_identity='opencode'""",
                (exhausted_chat,),
            ).fetchone()
            self.assertEqual(
                (reset["status"], reset["phase"], reset["attempt_count"]), ("pending", "set", 0)
            )
        finally:
            state.close()

    def test_successful_final_set_attempt_gets_a_fresh_verify_budget(self) -> None:
        chat_id = -1002222222222
        self.add_dynamic_binding("final-set", chat_id)
        state = HubState.open(self.config.state_path)
        try:
            store = ProjectOnboardingStore(state)
            store.ensure_command_scope_tasks(("opencode",))
            state._connection.execute(
                """UPDATE project_command_scopes SET attempt_count=19,total_attempt_count=19
                   WHERE telegram_chat_id=? AND bot_identity='opencode'""",
                (chat_id,),
            )
            state._connection.commit()
            setting = store.claim_command_scope("sender", ("opencode",))
            assert setting is not None
            self.assertEqual((setting.phase, setting.attempt_count), ("set", 20))
            store.advance_command_scope_to_verify(setting)
            verifying = store.claim_command_scope("sender", ("opencode",))
            assert verifying is not None
            self.assertEqual((verifying.phase, verifying.attempt_count), ("verify", 1))
            self.assertEqual(verifying.total_attempt_count, 21)
            store.complete_command_scope(verifying)
        finally:
            state.close()

    def test_repeated_set_verify_mismatch_has_a_total_attempt_limit(self) -> None:
        chat_id = -1002222222222
        self.add_dynamic_binding("bounded-mismatch", chat_id)
        state = HubState.open(self.config.state_path)
        try:
            store = ProjectOnboardingStore(state)
            store.ensure_command_scope_tasks(("opencode",))
            state._connection.execute(
                """UPDATE project_command_scopes SET phase='verify',total_attempt_count=39
                   WHERE telegram_chat_id=? AND bot_identity='opencode'""",
                (chat_id,),
            )
            state._connection.commit()
            verifying = store.claim_command_scope("sender", ("opencode",))
            assert verifying is not None
            self.assertEqual(verifying.total_attempt_count, 40)
            store.retry_command_scope(
                verifying,
                "ProjectCommandScopeMismatch",
                delay_seconds=0,
                restart_set=True,
            )
            exhausted = state._connection.execute(
                """SELECT status,phase,total_attempt_count FROM project_command_scopes
                   WHERE telegram_chat_id=? AND bot_identity='opencode'""",
                (chat_id,),
            ).fetchone()
            self.assertEqual(
                (exhausted["status"], exhausted["phase"], exhausted["total_attempt_count"]),
                ("failed", "set", 40),
            )
            store.reset_failed_command_scope(chat_id, "opencode")
        finally:
            state.close()

    def test_successful_set_at_total_limit_becomes_explicitly_resettable(self) -> None:
        chat_id = -1002222222222
        self.add_dynamic_binding("final-total-set", chat_id)
        state = HubState.open(self.config.state_path)
        try:
            store = ProjectOnboardingStore(state)
            store.ensure_command_scope_tasks(("opencode",))
            state._connection.execute(
                """UPDATE project_command_scopes SET attempt_count=5,total_attempt_count=39
                   WHERE telegram_chat_id=? AND bot_identity='opencode'""",
                (chat_id,),
            )
            state._connection.commit()
            setting = store.claim_command_scope("sender", ("opencode",))
            assert setting is not None
            self.assertEqual(setting.total_attempt_count, 40)
            store.advance_command_scope_to_verify(setting)
            exhausted = state._connection.execute(
                """SELECT status,phase,attempt_count,total_attempt_count,error_code
                   FROM project_command_scopes
                   WHERE telegram_chat_id=? AND bot_identity='opencode'""",
                (chat_id,),
            ).fetchone()
            self.assertEqual(
                tuple(exhausted),
                ("failed", "verify", 0, 40, "command_verify_budget_exhausted"),
            )
            store.reset_failed_command_scope(chat_id, "opencode")
        finally:
            state.close()

    def test_fair_polling_delivers_each_agent_with_its_own_bot(self) -> None:
        open_first = self.ready_outbox("opencode", 1)
        self.ready_outbox("opencode", 2)
        agy_first = self.ready_outbox("antigravity", 3)
        open_bot = Bot()
        agy_bot = Bot()
        sender = self.sender(opencode=open_bot, antigravity=agy_bot)
        try:
            startup = sender.state.get_runtime_health("sender", "test-sender")
            assert startup is not None
            self.assertEqual(startup.activity_state, "idle")
            self.assertTrue(sender.run_cycle())
            delivered = sender.state.get_runtime_health("sender", "test-sender")
            assert delivered is not None
            self.assertIsNotNone(delivered.success_at)
            self.assertIsNone(delivered.error_code)
            self.assertTrue(sender.run_cycle())
            self.assertEqual(len(open_bot.sent), 1)
            self.assertEqual(len(agy_bot.sent), 1)
            self.assertEqual(sender.state.get_provider_job(open_first).status, "completed")
            self.assertEqual(sender.state.get_provider_job(agy_first).status, "completed")
        finally:
            sender.close()

    def test_accepted_work_refreshes_provider_typing_indicator(self) -> None:
        self.ready_outbox("opencode", 9)
        open_bot = Bot()
        sender = self.sender(opencode=open_bot, antigravity=Bot())
        try:
            sender._refresh_chat_actions(now_monotonic=10.0)
            sender._refresh_chat_actions(now_monotonic=13.9)
            sender._refresh_chat_actions(now_monotonic=14.0)
            self.assertEqual(
                open_bot.actions,
                [
                    (-1001234567890, 79, "typing"),
                    (-1001234567890, 79, "typing"),
                ],
            )
        finally:
            sender.close()

    def test_private_work_refreshes_native_thinking_draft(self) -> None:
        state = HubState.open(self.config.state_path)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=123456789,
                thread_id=1,
                title="Direct",
            )
            session = state.activate_agent(topic.topic_id, "opencode", "model-1", "high")
            state.enqueue_provider_job(
                idempotency_key="telegram:123456789:44",
                chat_id=123456789,
                message_id=44,
                topic_id=topic.topic_id,
                agent_id="opencode",
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=None,
                model=session.model,
                effort=session.effort,
                payload_text="private task",
            )
        finally:
            state.close()
        open_bot = Bot()
        sender = self.sender(opencode=open_bot, antigravity=Bot())
        try:
            sender._refresh_chat_actions(now_monotonic=10.0)
            self.assertEqual(open_bot.drafts, [(123456789, 1, 44, "")])
            self.assertEqual(open_bot.actions, [(123456789, 1, "typing")])
        finally:
            sender.close()

    def test_transport_failure_retries_only_outbox_and_never_provider_work(self) -> None:
        job_id = self.ready_outbox("opencode", 10)
        sender = self.sender(opencode=Bot(fail=True), antigravity=Bot())
        try:
            self.assertTrue(sender.run_cycle())
            job = sender.state.get_provider_job(job_id)
            outbox = sender.state.get_telegram_outbox_for_job(job_id)
            self.assertEqual(job.status, "result_ready")
            self.assertEqual(outbox.status, "pending")
            self.assertEqual(outbox.attempt_count, 1)
            health = sender.state.get_runtime_health("sender", "test-sender")
            assert health is not None
            self.assertEqual(health.error_code, "RuntimeError")
            self.assertEqual(health.activity_state, "idle")
        finally:
            sender.close()

    def test_transport_health_threshold_recovers_after_outbox_retry(self) -> None:
        job_id = self.ready_outbox("opencode", 14)
        bot = TransportFailBot(fail=True)
        sender = self.sender(opencode=bot, antigravity=Bot())
        try:
            clock = datetime.now(timezone.utc)
            self.assertTrue(sender.run_cycle(now=clock))
            clock += timedelta(seconds=2)
            self.assertTrue(sender.run_cycle(now=clock))

            transient = sender.state.get_runtime_health("sender", "test-sender")
            assert transient is not None
            self.assertIsNone(transient.error_code)
            self.assertEqual(transient.transport_consecutive_failures, 2)
            self.assertEqual(
                sender.state.runtime_health_status("sender", "test-sender", now=clock).status,
                "healthy",
            )

            clock += timedelta(seconds=2)
            self.assertTrue(sender.run_cycle(now=clock))

            failed = sender.state.get_runtime_health("sender", "test-sender")
            assert failed is not None
            self.assertEqual(failed.error_code, "telegram_send_message_network_timeout")
            self.assertEqual(failed.transport_operation, "send_message")
            self.assertEqual(failed.transport_failure_class, "network_timeout")
            self.assertEqual(failed.transport_consecutive_failures, 3)
            self.assertEqual(
                sender.state.runtime_health_status("sender", "test-sender", now=clock).status,
                "degraded",
            )
            events = cast(
                list[dict[str, object]],
                sender.state.status_snapshot()["runtime_events"],
            )
            transport_events = [
                event for event in events if event["code"] == "telegram_transport_error"
            ]
            self.assertEqual(len(transport_events), 1)

            bot.fail = False
            # The third failed attempt now persists a four-second backoff.
            clock += timedelta(seconds=4)
            self.assertTrue(sender.run_cycle(now=clock))
            recovered = sender.state.get_runtime_health("sender", "test-sender")
            assert recovered is not None
            self.assertEqual(sender.state.get_provider_job(job_id).status, "completed")
            self.assertIsNone(recovered.error_code)
            self.assertIsNone(recovered.transport_operation)
            self.assertEqual(recovered.transport_consecutive_failures, 0)
            self.assertIsNotNone(recovered.transport_success_at)
            events = cast(
                list[dict[str, object]],
                sender.state.status_snapshot()["runtime_events"],
            )
            self.assertEqual(
                len([event for event in events if event["code"] == "telegram_recovered"]),
                1,
            )
        finally:
            sender.close()

    def test_multipart_retry_resumes_at_first_undelivered_part(self) -> None:
        long_html = ("part " * 3999) + "part"
        job_id = self.ready_outbox("opencode", 12, telegram_html=long_html)
        state = HubState.open(self.config.state_path)
        try:
            outbox = state.get_telegram_outbox_for_job(job_id)
            self.assertEqual(
                state.get_provider_result(job_id).visible_response,
                long_html,
            )
            self.assertGreater(len(state.get_telegram_outbox_parts(outbox.outbox_id)), 1)
        finally:
            state.close()

        bot = Bot()
        sender = self.sender(opencode=bot, antigravity=Bot())
        try:
            clock = datetime.now(timezone.utc) + timedelta(seconds=1)
            self.assertTrue(sender.run_cycle(now=clock))
            after_first = sender.state.get_telegram_outbox_for_job(job_id)
            self.assertEqual(after_first.status, "pending")
            self.assertEqual(sender.state.get_provider_job(job_id).status, "result_ready")
            parts = sender.state.get_telegram_outbox_parts(after_first.outbox_id)
            self.assertIsNotNone(parts[0].telegram_message_id)
            self.assertIsNone(parts[1].telegram_message_id)

            bot.fail = True
            clock += timedelta(seconds=1)
            self.assertTrue(sender.run_cycle(now=clock))
            after_failure = sender.state.get_telegram_outbox_for_job(job_id)
            self.assertEqual(after_failure.status, "pending")
            self.assertEqual(len(bot.sent), 2)

            bot.fail = False
            while sender.state.get_provider_job(job_id).status != "completed":
                clock += timedelta(seconds=2)
                self.assertTrue(sender.run_cycle(now=clock))
            self.assertEqual(bot.sent[1], bot.sent[2])
            self.assertNotEqual(bot.sent[0][2], bot.sent[-1][2])
            delivered = sender.state.get_telegram_outbox_parts(after_failure.outbox_id)
            self.assertTrue(all(part.telegram_message_id is not None for part in delivered))
        finally:
            sender.close()

    def test_successful_idle_cycle_clears_prior_cycle_error(self) -> None:
        sender = self.sender(opencode=Bot(), antigravity=Bot())
        calls = 0

        def fail_then_recover() -> bool:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary SQLite failure")
            sender.stop()
            return False

        sender.run_cycle = fail_then_recover  # type: ignore[method-assign]
        try:
            sender.run_forever(poll_seconds=0.001)
            health = sender.state.get_runtime_health("sender", "test-sender")
            assert health is not None
            self.assertEqual(calls, 2)
            self.assertIsNone(health.error_code)
            self.assertIsNotNone(health.success_at)
        finally:
            sender.close()

    def test_stop_after_delivery_lease_returns_unsent_row_without_attempt(self) -> None:
        job_id = self.ready_outbox("opencode", 11)
        sender = self.sender(opencode=Bot(), antigravity=Bot())
        original_lease = sender.state.lease_telegram_outbox

        def lease_then_stop(*args: object, **kwargs: object) -> object:
            leased = cast(Any, original_lease)(*args, **kwargs)
            sender.stop()
            return leased

        sender.state.lease_telegram_outbox = lease_then_stop  # type: ignore[method-assign]
        try:
            self.assertFalse(sender.run_cycle())
            outbox = sender.state.get_telegram_outbox_for_job(job_id)
            self.assertEqual((outbox.status, outbox.attempt_count), ("pending", 0))
        finally:
            sender.close()

    def test_sender_opens_every_local_queue_agent_token_in_mixed_rollout(self) -> None:
        open_token = Path(self.tempdir.name) / "opencode.token"
        agy_token = Path(self.tempdir.name) / "antigravity.token"
        open_token.write_text("123456:example", encoding="utf-8")
        agy_token.write_text("654321:example", encoding="utf-8")
        open_token.chmod(0o600)
        agy_token.chmod(0o600)
        mixed = replace(
            self.config,
            agents=(
                replace(self.config.require_agent("opencode"), token_file=open_token),
                replace(
                    self.config.require_agent("antigravity"),
                    token_file=agy_token,
                ),
            ),
            external_worker_agent_ids=("opencode",),
        )
        sender = TelegramOutboxSender(mixed)
        try:
            self.assertEqual(set(sender.telegram_bots), {"opencode", "antigravity"})
        finally:
            sender.close()

    def test_mixed_rollout_separates_execution_and_delivery_recovery_ownership(self) -> None:
        open_job = self.ready_outbox("opencode", 40)
        agy_job = self.ready_outbox("antigravity", 41)
        state = HubState.open(self.config.state_path)
        try:
            for agent_id in ("opencode", "antigravity"):
                leased = state.lease_telegram_outbox(agent_id, f"lost-{agent_id}")
                assert leased is not None
                state._connection.execute(
                    "UPDATE telegram_outbox SET lease_expires_at = ? WHERE outbox_id = ?",
                    ("2000-01-01T00:00:00+00:00", leased.outbox_id),
                )
                state._connection.commit()
        finally:
            state.close()

        mixed = replace(self.config, external_worker_agent_ids=("opencode",))
        embedded_bot = Bot()
        embedded_identity = cast(Any, type("SenderIdentity", (), {})())
        embedded_identity.telegram = embedded_bot
        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = mixed
        controller.external_services = {"antigravity": embedded_identity}
        controller.telegram = Bot()

        self.assertFalse(controller.run_embedded_queue_cycle())
        state = HubState.open(self.config.state_path)
        try:
            self.assertEqual(state.get_telegram_outbox_for_job(open_job).status, "sending")
            self.assertEqual(state.get_telegram_outbox_for_job(agy_job).status, "sending")
            self.assertEqual(embedded_bot.sent, [])
        finally:
            state.close()

        open_bot = Bot()
        agy_bot = Bot()
        sender = TelegramOutboxSender(
            mixed,
            telegram_bots=cast(dict[str, Any], {"opencode": open_bot, "antigravity": agy_bot}),
        )
        try:
            self.assertTrue(sender.run_cycle())
            self.assertTrue(sender.run_cycle())
            self.assertEqual(sender.state.get_provider_job(open_job).status, "completed")
            self.assertEqual(sender.state.get_provider_job(agy_job).status, "completed")
            self.assertEqual(len(open_bot.sent), 1)
            self.assertEqual(len(agy_bot.sent), 1)
        finally:
            sender.close()

    def test_sender_delivers_durable_stop_notice_through_hub_identity(self) -> None:
        token = Path(self.tempdir.name) / "hub.token"
        token.write_text("fictional-token", encoding="utf-8")
        token.chmod(0o600)
        config = replace(
            self.config,
            hub_bot=HubTelegramBot("example_hub_bot", token),
        )
        state = HubState.open(config.state_path)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=170,
                title="Stop",
            )
            session = state.activate_agent(topic.topic_id, "opencode", "model-1", "high")
            job, _ = state.enqueue_provider_job(
                idempotency_key="telegram:-1001234567890:170",
                chat_id=-1001234567890,
                message_id=170,
                topic_id=topic.topic_id,
                agent_id="opencode",
                session_id=session.session_id,
                session_generation=session.generation,
                model=session.model,
                effort=session.effort,
                payload_text="queued task",
            )
            request_id, _, _ = state.request_emergency_stop(
                topic_id=topic.topic_id,
                chat_id=-1001234567890,
                message_id=171,
                target_agent_id="opencode",
            )
            self.assertTrue(
                state.enqueue_emergency_stop_notice(
                    request_id, "Активной работы нет; отменено задач в очереди: 1."
                )
            )
        finally:
            state.close()

        bots = {"hub": Bot(), "opencode": Bot(), "antigravity": Bot()}
        sender = TelegramOutboxSender(config, telegram_bots=bots)
        try:
            self.assertTrue(sender.run_cycle())
            self.assertEqual(len(bots["hub"].sent), 1)
            self.assertEqual(bots["opencode"].sent, [])
            self.assertEqual(sender.state.get_provider_job(job.job_id).status, "cancelled")
        finally:
            sender.close()

    def test_mixed_embedded_execution_commits_without_controller_telegram_send(self) -> None:
        mixed = replace(self.config, external_worker_agent_ids=("opencode",))
        state = HubState.open(mixed.state_path)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=150,
                title="Mixed execution",
            )
            session = state.activate_agent(topic.topic_id, "antigravity", "model-1", "high")
            job, _ = state.enqueue_provider_job(
                idempotency_key="telegram:-1001234567890:150",
                chat_id=-1001234567890,
                message_id=150,
                topic_id=topic.topic_id,
                agent_id="antigravity",
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=None,
                model=session.model,
                effort=session.effort,
                payload_text="embedded provider task",
                context_watermark=None,
                handoff_id=None,
            )
        finally:
            state.close()

        embedded_bot = Bot()
        embedded_identity = cast(Any, type("SenderIdentity", (), {})())
        embedded_identity.telegram = embedded_bot
        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = mixed
        controller.external_services = {"antigravity": embedded_identity}
        controller.telegram = Bot()

        def execute(queue_state: HubState, leased_job: Any) -> None:
            executing = queue_state.mark_provider_job_executing(
                leased_job.job_id, leased_job.lease_token
            )
            assert executing.lease_token is not None
            queue_state.commit_provider_result(
                executing.job_id,
                executing.lease_token,
                visible_response="embedded result",
                sender_agent_id="antigravity",
                telegram_html="embedded result",
            )

        controller._execute_embedded_provider_job = execute
        self.assertTrue(controller.run_embedded_queue_cycle())
        self.assertEqual(embedded_bot.sent, [])

        agy_bot = Bot()
        sender = TelegramOutboxSender(
            mixed,
            telegram_bots=cast(dict[str, Any], {"opencode": Bot(), "antigravity": agy_bot}),
        )
        try:
            self.assertEqual(sender.state.get_provider_job(job.job_id).status, "result_ready")
            self.assertTrue(sender.run_cycle())
            self.assertEqual(sender.state.get_provider_job(job.job_id).status, "completed")
            self.assertEqual(len(agy_bot.sent), 1)
        finally:
            sender.close()

    def test_stale_recovery_is_scoped_to_configured_sender_agents(self) -> None:
        self.ready_outbox("opencode", 20)
        state = HubState.open(self.config.state_path)
        try:
            leased = state.lease_telegram_outbox("opencode", "lost-sender", lease_seconds=60)
            assert leased is not None
            state._connection.execute(
                """UPDATE telegram_outbox
                   SET sender_agent_id = 'legacy', lease_expires_at = ? WHERE outbox_id = ?""",
                (
                    (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                    leased.outbox_id,
                ),
            )
            state._connection.commit()
        finally:
            state.close()

        sender = self.sender(opencode=Bot(), antigravity=Bot())
        try:
            self.assertFalse(sender.run_cycle())
            stale = sender.state.get_telegram_outbox(leased.outbox_id)
            self.assertEqual(stale.status, "sending")
        finally:
            sender.close()

    def test_controller_external_mode_has_no_sender_loop_or_isolated_adapter(self) -> None:
        config = self.config
        token_file = Path(self.tempdir.name) / "codex.token"
        codex = AgentDefinition(
            "codex",
            "Codex",
            "example_codex_bot",
            "codex",
            token_file,
            True,
            False,
            "gpt-5.6-sol",
            "high",
        )
        token_file.write_text("123456:example", encoding="utf-8")
        token_file.chmod(0o600)
        config = replace(
            config,
            agents=(codex,) + config.agents,
            external_worker_agent_ids=("codex", "opencode", "antigravity"),
        )
        with (
            patch("hermes_codex_router.service.load_registry"),
            patch(
                "hermes_codex_router.service.ExternalAgentService",
                side_effect=AssertionError("isolated adapter constructed"),
            ),
        ):
            controller = ProjectHubService(config)
        try:
            health = controller.state.get_runtime_health("controller", "project-hub-controller")
            self.assertIsNotNone(health)
            controller._start_controller_outbox_delivery()
            self.assertIsNone(controller._outbox_thread)
            self.assertNotIn("opencode", controller.external_services)

            class OnePoll:
                def updates(self, *, offset: int | None, timeout: int) -> list[object]:
                    del offset, timeout
                    controller.stop()
                    return []

            controller.telegram = cast(Any, OnePoll())
            controller.run_forever()
            heartbeat = controller.state.get_runtime_health("controller", "project-hub-controller")
            assert heartbeat is not None
            self.assertIsNotNone(heartbeat.success_at)
            self.assertIsNone(heartbeat.error_code)
        finally:
            controller.close()

    def test_controller_outbox_runtime_remains_backward_compatible_by_default(self) -> None:
        job_id = self.ready_outbox("opencode", 30)
        bot = Bot()
        service = cast(Any, type("SenderIdentity", (), {})())
        service.telegram = bot
        controller = cast(Any, ProjectHubService.__new__(ProjectHubService))
        controller.config = replace(self.config, outbox_runtime="controller")
        controller.external_services = {"opencode": service}
        controller.telegram = Bot()
        controller._outbox_agent_cursor = 0

        self.assertTrue(controller.run_controller_outbox_cycle())
        state = HubState.open(self.config.state_path)
        try:
            self.assertEqual(state.get_provider_job(job_id).status, "completed")
            self.assertEqual(len(bot.sent), 1)
        finally:
            state.close()

    def test_cli_sender_uses_standalone_sender(self) -> None:
        with (
            patch("hermes_codex_router.cli.load_outbox_sender_config", return_value=self.config),
            patch("hermes_codex_router.cli.TelegramOutboxSender") as sender_type,
        ):
            sender_type.return_value.run_forever.return_value = None
            self.assertEqual(main(["sender", "example.json", "--poll-seconds", "0.5"]), 0)
        sender_type.assert_called_once_with(self.config)
        sender_type.return_value.run_forever.assert_called_once_with(poll_seconds=0.5)
        sender_type.return_value.close.assert_called_once_with()

    def test_outbox_delivers_artifact_documents_in_order(self) -> None:
        from hermes_codex_router.artifacts import artifact_spool_root, spool_staged_artifacts

        staging = Path(self.tempdir.name) / ".hub" / "staging" / "artifact-test"
        staging.mkdir(parents=True)
        (staging / "test_report.md").write_text("# Test Report", encoding="utf-8")
        artifact = spool_staged_artifacts(
            Path(self.tempdir.name),
            "artifact-test",
            artifact_spool_root(self.config.state_path),
        )[0]

        state = HubState.open(self.config.state_path)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=88,
                title="Example",
            )
            session = state.activate_agent(topic.topic_id, "antigravity", "gpt-5.6-sol", "high")
            job, _ = state.enqueue_provider_job(
                idempotency_key="telegram:-1001234567890:88",
                chat_id=-1001234567890,
                message_id=88,
                topic_id=topic.topic_id,
                agent_id="antigravity",
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=None,
                model="gpt-5.6-sol",
                effort="high",
                payload_text="generate report",
            )
            leased_job = state.lease_provider_job("antigravity", "worker-1")
            assert leased_job is not None and leased_job.lease_token is not None
            state.mark_provider_job_executing(leased_job.job_id, leased_job.lease_token)
            state.commit_provider_result(
                leased_job.job_id,
                leased_job.lease_token,
                visible_response="Done!",
                sender_agent_id="antigravity",
                telegram_html="Done!",
                artifacts=(artifact,),
            )
        finally:
            state.close()

        bots = {"opencode": Bot(), "antigravity": Bot()}
        sender = TelegramOutboxSender(self.config, telegram_bots=bots)
        try:
            # Deliver text part
            self.assertTrue(sender.run_cycle())
            self.assertEqual(len(bots["antigravity"].sent), 1)
            self.assertEqual(len(bots["antigravity"].documents), 0)

            # Deliver document part
            self.assertTrue(sender.run_cycle())
            self.assertEqual(len(bots["antigravity"].documents), 1)
            self.assertEqual(bots["antigravity"].documents[0][2], artifact.path)
            self.assertFalse(artifact.path.exists())

            # No more parts to deliver
            self.assertFalse(sender.run_cycle())
        finally:
            sender.close()

    def test_outbox_rejects_spool_content_changed_after_commit(self) -> None:
        from hermes_codex_router.artifacts import artifact_spool_root, spool_staged_artifacts

        staging = Path(self.tempdir.name) / ".hub" / "staging" / "tamper-test"
        staging.mkdir(parents=True)
        (staging / "report.md").write_text("original", encoding="utf-8")
        artifact = spool_staged_artifacts(
            Path(self.tempdir.name),
            "tamper-test",
            artifact_spool_root(self.config.state_path),
        )[0]
        state = HubState.open(self.config.state_path)
        try:
            topic = state.observe_topic(
                project_id="example-project",
                chat_id=-1001234567890,
                thread_id=89,
                title="Example",
            )
            session = state.activate_agent(topic.topic_id, "antigravity", "model", "high")
            job, _ = state.enqueue_provider_job(
                idempotency_key="telegram:-1001234567890:89",
                chat_id=-1001234567890,
                message_id=89,
                topic_id=topic.topic_id,
                agent_id="antigravity",
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=None,
                model="model",
                effort="high",
                payload_text="generate report",
            )
            leased = state.lease_provider_job("antigravity", "worker-1")
            assert leased is not None and leased.lease_token is not None
            state.mark_provider_job_executing(job.job_id, leased.lease_token)
            state.commit_provider_result(
                job.job_id,
                leased.lease_token,
                visible_response="Done",
                sender_agent_id="antigravity",
                telegram_html="Done",
                artifacts=(artifact,),
            )
        finally:
            state.close()

        artifact.path.write_text("tampered", encoding="utf-8")
        bot = Bot()
        sender = TelegramOutboxSender(
            self.config,
            telegram_bots={"opencode": Bot(), "antigravity": bot},
        )
        try:
            self.assertTrue(sender.run_cycle())  # text
            self.assertTrue(sender.run_cycle())  # rejected document attempt
            self.assertEqual(bot.documents, [])
            outbox = sender.state.get_telegram_outbox_for_job(job.job_id)
            self.assertEqual(outbox.status, "pending")
            self.assertEqual(outbox.error_code, "ArtifactSecurityError")
        finally:
            sender.close()


if __name__ == "__main__":
    unittest.main()
