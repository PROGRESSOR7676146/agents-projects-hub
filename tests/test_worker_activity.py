from __future__ import annotations

import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from hermes_codex_router.codex_activity import CodexActivityEvent
from hermes_codex_router.state import StateError
from hermes_codex_router.worker_activity import codex_activity_for_turn
from tests import test_task_activity


class WorkerActivityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_task_activity.TaskActivityTests()
        self.fixture.setUp()
        self.client: Any = SimpleNamespace(on_activity=None)
        self.state: Any = SimpleNamespace(
            _connection=self.fixture.db, _immediate_transaction=self.fixture.transaction
        )
        self.config: Any = SimpleNamespace(
            hub_bot=object(), queue_runtime="external", outbox_runtime="external"
        )
        self.now = self.fixture.now

    def tearDown(self):
        self.fixture.tearDown()

    def observe(self):
        return codex_activity_for_turn(
            self.client,
            self.state,
            self.config,
            "job",
            "lease",
            Path("/home/example/project"),
            clock=lambda: self.now,
        )

    def test_acceptance_binds_before_events_and_passive_notice_is_persisted(self):
        with self.observe() as accepted:
            self.assertIsNotNone(self.client.on_activity)
            self.assertIsNone(self.fixture.row())
            accepted("thread", "turn")
            self.client.on_activity(
                CodexActivityEvent("tool_started", "command", "thread", "turn", "item")
            )
            self.now += timedelta(seconds=1)
            self.client.on_activity(
                CodexActivityEvent("approval_requested", "command", "thread", "turn", "item", 12)
            )
            self.assertEqual(self.fixture.row()["mode"], "approval")
            self.now += timedelta(seconds=1)
            self.client.on_activity(
                CodexActivityEvent("approval_resolved", "command", "thread", "turn", "item", 12)
            )
            self.assertEqual(len(self.fixture.evaluate(1202)), 1)
        self.assertIsNone(self.client.on_activity)

    def test_start_failure_clears_callback_without_binding_or_notice(self):
        with self.assertRaises(RuntimeError):
            with self.observe():
                raise RuntimeError("start failed")
        self.assertIsNone(self.client.on_activity)
        self.assertIsNone(self.fixture.row())

    def test_wait_failure_clears_callback_retaining_accepted_identity(self):
        with self.assertRaises(RuntimeError):
            with self.observe() as accepted:
                accepted("thread", "turn")
                raise RuntimeError("wait failed")
        self.assertIsNone(self.client.on_activity)
        self.assertEqual(self.fixture.row()["provider_turn_id"], "turn")

    def test_disabled_paths_do_not_install_or_bind(self):
        for field, value in [
            ("hub_bot", None),
            ("queue_runtime", "embedded"),
            ("outbox_runtime", "embedded"),
        ]:
            prior = getattr(self.config, field)
            setattr(self.config, field, value)
            with self.observe() as accepted:
                self.assertIsNone(self.client.on_activity)
                accepted("thread", "turn")
            self.assertIsNone(self.fixture.row())
            setattr(self.config, field, prior)

    def test_binding_failure_clears_callback_without_replay(self):
        starts = 0
        with self.assertRaises(StateError):
            with self.observe() as accepted:
                starts += 1
                accepted("thread", "wrong-turn")
        self.assertEqual(starts, 1)
        self.assertIsNone(self.client.on_activity)
        self.assertIsNone(self.fixture.row())

    def test_unaccepted_early_event_cannot_create_notice(self):
        with self.assertRaises(StateError):
            with self.observe():
                self.client.on_activity(
                    CodexActivityEvent("approval_requested", "command", "thread", "turn", "item", 1)
                )
        self.assertIsNone(self.fixture.row())
        self.assertEqual(
            self.fixture.db.execute("SELECT count(*) FROM task_lifecycle_notices").fetchone()[0], 0
        )
