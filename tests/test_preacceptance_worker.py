from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

from hermes_codex_router.codex_appserver import CodexAppServerClient
from hermes_codex_router.codex_worker import CodexQueueWorker
from hermes_codex_router.hub_config import AgentDefinition, HubTelegramBot
from hermes_codex_router.outbox_sender import TelegramOutboxSender
from hermes_codex_router.state import HubState
from hermes_codex_router.task_notice_sender import deliver_task_notice
from hermes_codex_router.worker_activity import codex_activity_for_turn
from tests import test_codex_activity_client as native
from tests import test_codex_worker as worker_fixtures
from tests import test_outbox_sender as sender_fixtures
from tests import test_preacceptance_approvals as fixtures
from tests.test_outbox_sender import Bot


class PreacceptanceWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.PreacceptanceApprovalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config: Any = SimpleNamespace(
            hub_bot=object(), queue_runtime="external", outbox_runtime="external"
        )

    def observe(self, client):
        f = self.fixture
        return codex_activity_for_turn(
            client,
            f.state,
            self.config,
            f.job.job_id,
            f.token,
            f.root,
            runtime_epoch=f.runtime,
            prepared_thread_id="example-thread",
            clock=lambda: f.now,
        )

    def sender(self, bot: Bot) -> TelegramOutboxSender:
        f = self.fixture
        config_fixture = sender_fixtures.TelegramOutboxSenderTests()
        config_fixture.setUp()
        self.addCleanup(config_fixture.tearDown)
        config = replace(
            config_fixture.config,
            state_path=f.root / "state.db",
            hub_bot=HubTelegramBot("example_hub_bot", f.root / "unused-token"),
            agents=(
                AgentDefinition(
                    "codex",
                    "Codex",
                    "example_codex_bot",
                    "codex",
                    None,
                    False,
                    False,
                    "example-model",
                    "high",
                ),
            ),
            external_worker_agent_ids=("codex",),
            codex_permission_profile=None,
        )
        sender = TelegramOutboxSender(config, telegram_bots={"hub": bot, "codex": Bot()})
        self.addCleanup(sender.state.close)
        return sender

    def test_actual_outbox_sender_delivers_early_notice_and_preserves_receipt(self) -> None:
        f = self.fixture
        scope = f.scope()
        f.observe(scope)
        bot = Bot()
        sender = self.sender(bot)
        self.assertTrue(sender._deliver_task_notice_one(now=f.now))
        self.assertEqual(bot.sent[0][:2], (f.topic.chat_id, f.topic.thread_id))
        self.assertEqual(f.notice()["status"], "delivered")
        self.assertEqual(f.notice()["telegram_message_id"], 1)
        checkpoint = f.journal.read(f.job.job_id)
        assert checkpoint is not None
        self.assertIsNone(checkpoint["provider_turn_id"])
        self.assertFalse(sender._deliver_task_notice_one(now=f.now))

    def test_actual_outbox_sender_suppresses_stale_early_notice(self) -> None:
        f = self.fixture
        scope = f.scope()
        f.observe(scope)
        bot = Bot()
        sender = self.sender(bot)
        with f.state._connection:
            f.state._connection.execute("UPDATE agent_sessions SET writer_mode='local'")
        self.assertTrue(sender._deliver_task_notice_one(now=f.now))
        self.assertEqual(bot.sent, [])
        self.assertEqual(f.notice()["status"], "superseded")
        self.assertEqual(f.state.get_provider_job(f.job.job_id).lease_token, f.token)

    def test_actual_worker_registers_once_at_startup_and_not_on_idle_cycles(self) -> None:
        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        config = replace(
            fixture.config,
            hub_bot=HubTelegramBot(
                "example_hub_bot", fixture.config.state_path.parent / "unused-token"
            ),
            outbox_runtime="external",
        )
        first = CodexQueueWorker(
            config,
            registry=fixture.registry,
            supervisor=cast(Any, worker_fixtures.WorkerSupervisor(worker_fixtures.WorkerClient())),
        )
        try:
            epoch = first._preacceptance_epoch
            assert epoch is not None
            self.assertEqual(epoch.epoch, 1)
            for _ in range(3):
                self.assertFalse(first.run_cycle())
            self.assertEqual(first._preacceptance_epoch, epoch)
            row = first.state._connection.execute(
                "SELECT * FROM preacceptance_runtime_epochs"
            ).fetchone()
            self.assertEqual((row["epoch"], row["instance_token"]), (1, epoch.instance_token))
        finally:
            first.close()
        second = CodexQueueWorker(
            config,
            registry=fixture.registry,
            supervisor=cast(Any, worker_fixtures.WorkerSupervisor(worker_fixtures.WorkerClient())),
        )
        try:
            next_epoch = second._preacceptance_epoch
            assert next_epoch is not None
            self.assertEqual(next_epoch.epoch, 2)
            self.assertNotEqual(next_epoch.instance_token, epoch.instance_token)
        finally:
            second.close()

    def test_real_client_and_second_connection_sender_deliver_before_start_response(self) -> None:
        f = self.fixture
        bot = Bot()
        owner = self

        class GateTransport(native.Transport):
            def receive(self, *, timeout=None):
                message = super().receive(timeout=timeout)
                if "result" in message:
                    checkpoint = f.journal.read(f.job.job_id)
                    assert checkpoint is not None
                    owner.assertIsNone(checkpoint["provider_turn_id"])
                    with closing(
                        HubState.open(f.root / "state.db", codex_permission_profile=None)
                    ) as sender:
                        owner.assertTrue(
                            deliver_task_notice(
                                sender.task_notices, bot, "example-sender", now=f.now
                            ).delivered
                        )
                    owner.assertEqual(len(bot.sent), 1)
                return message

        transport = GateTransport(
            [
                native.approval(thread="example-thread", turn="example-turn"),
                native.started_response(turn="example-turn"),
                native.resolved(thread="example-thread"),
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "example-thread",
                        "turn": {"id": "example-turn", "status": "completed"},
                    },
                },
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)
        with self.observe(client) as accepted:
            turn = client.start_turn(
                thread_id="example-thread",
                cwd=f.root,
                text="Example task",
                model="example-model",
                effort="high",
            )
            f.journal.record_turn(f.job.job_id, f.token, turn)
            accepted("example-thread", turn)
            client.wait_for_turn(turn)
        self.assertIsNone(client.on_activity)
        self.assertIsNone(client.on_preacceptance_approval)
        self.assertEqual(len(bot.sent), 1)
        self.assertEqual(
            f.state._connection.execute("SELECT count(*) FROM task_activity_entries").fetchone()[0],
            1,
        )
        self.assertEqual(
            f.state._connection.execute("SELECT state FROM task_activity_entries").fetchone()[0],
            "resolved",
        )

    def test_failed_start_retires_observations_and_always_clears_callbacks(self) -> None:
        f = self.fixture
        client = CodexAppServerClient(
            native.Transport([native.approval(thread="example-thread", turn="example-turn")]),
            initialized=True,
        )
        with self.assertRaises(EOFError):
            with self.observe(client):
                client.start_turn(
                    thread_id="example-thread",
                    cwd=f.root,
                    text="Example task",
                    model="example-model",
                    effort="high",
                )
        self.assertIsNone(client.on_activity)
        self.assertIsNone(client.on_preacceptance_approval)
        checkpoint = f.journal.read(f.job.job_id)
        assert checkpoint is not None
        self.assertIsNone(checkpoint["provider_turn_id"])
        self.assertEqual(f.notice()["status"], "superseded")
        self.assertEqual(
            f.state._connection.execute("SELECT count(*) FROM task_activity").fetchone()[0], 0
        )

    def test_promotion_failure_rolls_back_accepted_activity_binding(self) -> None:
        f = self.fixture
        client: Any = SimpleNamespace(on_activity=None, on_preacceptance_approval=None)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.observe(client) as accepted:
                client.on_preacceptance_approval(f.event())
                f.journal.record_turn(f.job.job_id, f.token, "example-turn")
                with f.state._connection:
                    f.state._connection.execute(
                        "CREATE TRIGGER example_import_failure BEFORE INSERT ON task_activity_entries "
                        "BEGIN SELECT RAISE(ABORT,'example import failure'); END"
                    )
                accepted("example-thread", "example-turn")
        self.assertIsNone(client.on_activity)
        self.assertIsNone(client.on_preacceptance_approval)
        self.assertEqual(
            f.state._connection.execute("SELECT count(*) FROM task_activity").fetchone()[0], 0
        )
        self.assertEqual(
            f.state._connection.execute("SELECT state FROM preacceptance_scopes").fetchone()[0],
            "retired",
        )


if __name__ == "__main__":
    unittest.main()
