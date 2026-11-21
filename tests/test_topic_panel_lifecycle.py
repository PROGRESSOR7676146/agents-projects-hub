"""Topic panel lifecycle, send evidence and sender behavior (ADR 0110).

Real temporary SQLite and a fictional Telegram API; no network or provider.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hermes_codex_router.state import HubState
from hermes_codex_router.telegram import TelegramError
from hermes_codex_router.topic_panel_sender import (
    PANEL_ACTIVE_SECONDS,
    PANEL_IDLE_SECONDS,
    TopicPanelSender,
)
from hermes_codex_router.topic_panel_state import (
    PANEL_SEND_LEASE_SECONDS,
    PANEL_SETTLED_RETENTION,
    TopicPanelStore,
)

CHAT = -1001234567890
NAMES = {"claude": "Claude", "codex": "Codex"}


def rejection(status: int, **extra: Any) -> TelegramError:
    return TelegramError(
        "fictional rejection",
        operation="api_call",
        failure_class="api_rejection",
        status_code=status,
        **extra,
    )


class PanelApi:
    """A fictional Bot API recording every panel request."""

    def __init__(self) -> None:
        self.creates: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.errors: list[Exception] = []
        self.during: Any = None
        self.next_message_id = 500

    def send_html(
        self,
        chat_id: int,
        thread_id: int,
        html: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        reply_to_message_id: int | None = None,
        disable_notification: bool = False,
    ) -> int:
        self.creates.append(
            {
                "chat_id": chat_id,
                "thread_id": thread_id,
                "html": html,
                "reply_markup": reply_markup,
                "reply_to": reply_to_message_id,
                "silent": disable_notification,
            }
        )
        self._effect()
        self.next_message_id += 1
        return self.next_message_id

    def call(self, method: str, **params: object) -> object:
        if method != "editMessageText":
            raise AssertionError(method)
        self.edits.append(dict(params))
        self._effect()
        return {"message_id": params["message_id"], "text": "fictional"}

    @property
    def requests(self) -> int:
        return len(self.creates) + len(self.edits)

    def _effect(self) -> None:
        if self.during is not None:
            during, self.during = self.during, None
            during()
        if self.errors:
            raise self.errors.pop(0)


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class TopicPanelLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        base = Path(directory.name)
        root = base / "example-project"
        root.mkdir()
        self.topic_root = root
        self.state = HubState.open(base / "state.db", codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.db = self.state._connection
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=CHAT,
            thread_id=7,
            title="Fictional topic",
            execution_root=root,
        )
        self.session = self.state.activate_agent(
            self.topic.topic_id, "claude", "example-model", "high"
        )
        self.store = TopicPanelStore(self.state)
        self.api = PanelApi()
        self.clock = Clock()
        self.successes = 0
        self.failures: list[TelegramError] = []
        self.events: list[tuple[str, str, str]] = []
        self.sender = TopicPanelSender(
            self.state,
            self.api,
            agent_names=NAMES,
            transport_success=self._success,
            transport_failure=self.failures.append,
            record_event=lambda *event: self.events.append(event),
            chat_registered=self._registered,
            clock=self.clock,
        )
        self.now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
        self.message_id = 0
        self.registered = {CHAT}
        self.before_send: Any = None

    def _success(self) -> None:
        self.successes += 1

    def _registered(self, chat_id: int) -> bool:
        if self.before_send is not None:
            self.before_send()
        return chat_id in self.registered

    def cycle(self, *, seconds: float = 2.0) -> bool:
        """Advance both clocks past the scan interval, then run one sender step."""
        self.clock.value += seconds
        self.now += timedelta(seconds=seconds)
        return self.sender.deliver_one(now=self.now)

    def enqueue(self) -> None:
        self.message_id += 1
        self.state.enqueue_provider_job(
            idempotency_key=f"example:{self.message_id}",
            chat_id=CHAT,
            message_id=self.message_id,
            topic_id=self.topic.topic_id,
            agent_id="claude",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model="example-model",
            effort="high",
            payload_text="Fictional request",
        )

    def panel(self) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM topic_panels WHERE topic_id=?", (self.topic.topic_id,)
        ).fetchone()
        assert row is not None
        return row

    def operations(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM topic_panel_operations ORDER BY send_started_at,rowid"
        ).fetchall()

    def open_and_create(self) -> int:
        self.store.open(self.topic, now=self.now)
        self.assertTrue(self.cycle())
        return int(self.panel()["message_id"])

    def test_create_then_edit_only_when_the_displayed_body_changes(self) -> None:
        instance = self.store.open(self.topic, now=self.now)
        self.assertTrue(self.cycle())
        create = self.api.creates[0]
        self.assertEqual((create["chat_id"], create["thread_id"]), (CHAT, 7))
        self.assertTrue(create["silent"])
        self.assertIn("Нет активных задач", create["html"])
        self.assertIn("Последнее изменение: 20.11 15:00", create["html"])
        self.assertEqual(
            create["reply_markup"],
            {"inline_keyboard": [[{"text": "Обновить", "callback_data": f"pn:r:{instance}"}]]},
        )
        panel = self.panel()
        self.assertEqual(panel["message_id"], 501)
        self.assertEqual(self.successes, 1)
        self.assertEqual([row["state"] for row in self.operations()], ["confirmed"])

        # Unchanged: later checks make no HTTP request, even well past the cadence.
        for _ in range(3):
            self.assertFalse(self.cycle(seconds=PANEL_IDLE_SECONDS + 1))
        self.assertEqual(self.api.requests, 1)

        self.enqueue()
        self.assertTrue(self.cycle(seconds=PANEL_IDLE_SECONDS + 1))
        edit = self.api.edits[0]
        self.assertEqual((edit["chat_id"], edit["message_id"]), (CHAT, 501))
        self.assertEqual(edit["parse_mode"], "HTML")
        self.assertIn("В очереди", str(edit["text"]))
        self.assertNotEqual(self.panel()["confirmed_digest"], panel["confirmed_digest"])
        # Active topics are checked at the shorter cadence.
        self.assertEqual(
            self.panel()["next_due_at"],
            (self.now + timedelta(seconds=PANEL_ACTIVE_SECONDS)).isoformat(timespec="microseconds"),
        )

    def test_unchanged_refresh_is_served_without_http_and_a_newer_press_stays_due(self) -> None:
        message_id = self.open_and_create()
        instance = int(self.panel()["instance"])

        def press() -> str:
            return self.store.request_refresh(
                chat_id=CHAT, thread_id=7, message_id=message_id, instance=instance, now=self.now
            )

        self.assertEqual(press(), "accepted")
        self.assertEqual(press(), "pending")
        self.assertFalse(self.cycle())
        self.assertEqual(self.api.requests, 1)
        self.assertIsNone(self.panel()["refresh_requested_at"])

        # A press that arrives after the check read the panel is not lost.
        panel = self.store.due(now=self.now + timedelta(days=1))[0]
        self.assertEqual(press(), "accepted")
        self.assertTrue(
            self.store.defer_unchanged(
                panel, next_due_at=self.now + timedelta(minutes=5), now=self.now
            )
        )
        self.assertIsNotNone(self.panel()["refresh_requested_at"])
        self.assertLessEqual(self.panel()["next_due_at"], self.panel()["refresh_requested_at"])

    def test_stale_presses_change_nothing(self) -> None:
        message_id = self.open_and_create()
        instance = int(self.panel()["instance"])
        before = tuple(self.panel())
        for stale_message, stale_instance in (
            (message_id + 1, instance),
            (message_id, instance + 1),
        ):
            self.assertEqual(
                self.store.request_refresh(
                    chat_id=CHAT,
                    thread_id=7,
                    message_id=stale_message,
                    instance=stale_instance,
                    now=self.now,
                ),
                "stale",
            )
        self.assertEqual(
            self.store.request_refresh(
                chat_id=CHAT, thread_id=8, message_id=message_id, instance=instance, now=self.now
            ),
            "stale",
        )
        self.assertEqual(tuple(self.panel()), before)
        self.assertTrue(self.store.close(self.topic.topic_id, now=self.now))
        self.assertFalse(self.store.close(self.topic.topic_id, now=self.now))
        self.assertEqual(
            self.store.request_refresh(
                chat_id=CHAT, thread_id=7, message_id=message_id, instance=instance, now=self.now
            ),
            "stale",
        )
        self.assertFalse(self.cycle(seconds=PANEL_IDLE_SECONDS + 1))
        self.assertEqual(self.api.requests, 1)

    def test_repeated_panel_always_creates_a_new_message(self) -> None:
        self.open_and_create()
        second = self.store.open(self.topic, now=self.now)
        self.assertTrue(self.cycle())
        self.assertEqual(len(self.api.creates), 2)
        self.assertEqual(self.panel()["message_id"], 502)
        self.assertEqual(
            self.api.creates[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"],
            f"pn:r:{second}",
        )

    def test_replacement_during_create_never_revives_the_old_instance(self) -> None:
        self.store.open(self.topic, now=self.now)
        self.api.during = lambda: self.store.open(self.topic, now=self.now)
        self.assertTrue(self.cycle())
        panel = self.panel()
        self.assertEqual(panel["instance"], 2)
        self.assertIsNone(panel["message_id"])
        self.assertEqual(self.operations()[0]["state"], "confirmed")
        self.assertEqual(self.operations()[0]["receipt_message_id"], 501)
        self.assertTrue(self.cycle())
        self.assertEqual(self.panel()["message_id"], 502)
        self.assertEqual([row["instance"] for row in self.operations()], [1, 2])

    def test_disable_during_send_keeps_the_panel_off(self) -> None:
        self.store.open(self.topic, now=self.now)
        self.api.during = lambda: self.store.close(self.topic.topic_id, now=self.now)
        self.assertTrue(self.cycle())
        panel = self.panel()
        self.assertEqual((panel["enabled"], panel["message_id"]), (0, None))
        self.assertFalse(self.cycle(seconds=PANEL_IDLE_SECONDS + 1))
        self.assertEqual(self.api.requests, 1)

    def test_unknown_create_is_never_retried_until_a_new_panel(self) -> None:
        self.store.open(self.topic, now=self.now)
        self.api.errors.append(
            TelegramError("timeout", operation="send_message", failure_class="network_timeout")
        )
        self.assertTrue(self.cycle())
        self.assertEqual(self.operations()[0]["state"], "unknown")
        self.assertEqual(self.panel()["unavailable_reason"], "send_unknown")
        self.assertEqual(len(self.failures), 1)
        for _ in range(3):
            self.assertFalse(self.cycle(seconds=PANEL_IDLE_SECONDS + 1))
        self.assertEqual(self.api.requests, 1)
        self.store.open(self.topic, now=self.now)
        self.assertTrue(self.cycle())
        self.assertEqual(len(self.api.creates), 2)
        self.assertEqual([row["state"] for row in self.operations()], ["unknown", "confirmed"])

    def test_unknown_edit_stops_editing_that_message(self) -> None:
        self.open_and_create()
        self.enqueue()
        self.api.errors.append(
            TelegramError("server", operation="api_call", failure_class="api_http", status_code=502)
        )
        self.assertTrue(self.cycle(seconds=PANEL_IDLE_SECONDS + 1))
        self.assertEqual(self.panel()["unavailable_reason"], "send_unknown")
        self.enqueue()
        self.assertFalse(self.cycle(seconds=PANEL_IDLE_SECONDS + 1))
        self.assertEqual(len(self.api.edits), 1)

    def test_abandoned_send_becomes_unknown_and_a_late_receipt_changes_nothing(self) -> None:
        self.store.open(self.topic, now=self.now)
        panel = self.store.due(now=self.now)[0]
        operation = self.store.begin(
            panel,
            payload_html="Fictional",
            reply_markup={"inline_keyboard": []},
            body_digest="0" * 64,
            now=self.now,
        )
        assert operation is not None
        # The open operation holds the slot: nothing else is sent meanwhile.
        self.assertEqual(self.store.due(now=self.now + timedelta(days=1)), ())
        self.assertFalse(self.cycle(seconds=PANEL_SEND_LEASE_SECONDS - 5))
        self.assertFalse(self.sender.deliver_one(now=self.now + timedelta(seconds=10)))
        self.assertEqual(self.operations()[0]["state"], "unknown")
        self.assertEqual(self.operations()[0]["error_code"], "lease_expired")
        self.assertEqual(self.panel()["unavailable_reason"], "send_unknown")
        self.assertIn("topic_panel_send_unknown", [event[1] for event in self.events])
        self.assertFalse(
            self.store.confirm(
                operation, receipt_message_id=900, next_due_at=self.now, now=self.now
            )
        )
        self.assertIsNone(self.panel()["message_id"])
        self.assertEqual(self.api.requests, 0)

    def test_rate_limit_pauses_panel_requests_and_retries_the_proven_rejection(self) -> None:
        self.store.open(self.topic, now=self.now)
        self.api.errors.append(rejection(429, retry_after=30))
        self.assertTrue(self.cycle())
        self.assertEqual(self.operations()[0]["state"], "rejected")
        self.assertEqual(self.operations()[0]["error_code"], "api_429")
        self.assertEqual(len(self.failures), 1)
        self.assertIsNone(self.panel()["unavailable_reason"])
        self.assertFalse(self.cycle(seconds=20))
        self.assertEqual(self.api.requests, 1)
        self.assertTrue(self.cycle(seconds=15))
        self.assertEqual(len(self.api.creates), 2)
        self.assertEqual(self.panel()["message_id"], 501)

    def test_rate_limit_pause_survives_restart_new_panel_and_refresh(self) -> None:
        other = self.state.observe_topic(
            project_id="example-project",
            chat_id=CHAT,
            thread_id=8,
            title="Second fictional topic",
            execution_root=self.topic_root,
        )
        self.store.open(self.topic, now=self.now)
        self.store.open(other, now=self.now)
        self.assertTrue(self.cycle())
        self.assertTrue(self.cycle())
        shown = self.db.execute(
            "SELECT message_id,instance FROM topic_panels WHERE topic_id=?", (other.topic_id,)
        ).fetchone()
        self.store.open(self.topic, now=self.now)
        self.api.errors.append(rejection(429, retry_after=30))
        self.assertTrue(self.cycle())
        self.assertEqual(self.operations()[-1]["error_code"], "api_429")
        self.assertEqual(self.api.requests, 3)
        clock = Clock()
        restarted = TopicPanelSender(
            self.state,
            self.api,
            agent_names=NAMES,
            transport_success=self._success,
            transport_failure=self.failures.append,
            record_event=lambda *event: self.events.append(event),
            chat_registered=self._registered,
            clock=clock,
        )
        # Neither a restart, a new /panel nor a refresh shortens the pause.
        self.now += timedelta(seconds=10)
        self.store.open(self.topic, now=self.now)
        self.assertEqual(
            self.store.request_refresh(
                chat_id=CHAT,
                thread_id=8,
                message_id=int(shown["message_id"]),
                instance=int(shown["instance"]),
                now=self.now,
            ),
            "accepted",
        )
        for _ in range(3):
            self.now += timedelta(seconds=5)
            clock.value += 5
            self.assertFalse(restarted.deliver_one(now=self.now))
        self.assertEqual(self.api.requests, 3)
        self.now += timedelta(seconds=10)
        clock.value += 10
        self.assertTrue(restarted.deliver_one(now=self.now))
        self.assertEqual(self.api.requests, 4)

    def test_retention_never_prunes_the_last_confirmed_version(self) -> None:
        self.open_and_create()
        confirmed = self.operations()[0]["operation_id"]
        at = self.now + timedelta(days=1)
        for _ in range(PANEL_SETTLED_RETENTION + 5):
            at += timedelta(seconds=1)
            panel = self.store.due(now=at)[0]
            operation = self.store.begin(
                panel,
                payload_html="Fictional edit",
                reply_markup={"inline_keyboard": []},
                body_digest="a" * 64,
                now=at,
            )
            assert operation is not None
            self.store.reject(operation, error_code="not_sent", now=at, retry_at=at)
        identities = [row["operation_id"] for row in self.operations()]
        self.assertIn(confirmed, identities)
        self.assertEqual(len(identities), PANEL_SETTLED_RETENTION + 1)
        self.assertEqual(self.panel()["confirmed_operation_id"], confirmed)

    def test_snapshot_backoff_keeps_a_newer_press_due(self) -> None:
        message_id = self.open_and_create()
        panel = self.store.due(now=self.now + timedelta(days=1))[0]
        self.assertEqual(
            self.store.request_refresh(
                chat_id=CHAT,
                thread_id=7,
                message_id=message_id,
                instance=panel.instance,
                now=self.now,
            ),
            "accepted",
        )
        self.store.postpone(panel, next_due_at=self.now + timedelta(minutes=1), now=self.now)
        self.assertEqual(self.panel()["next_due_at"], self.panel()["refresh_requested_at"])

    def test_rate_limit_pause_counts_the_whole_cycle_before_the_response(self) -> None:
        self.store.open(self.topic, now=self.now)
        self.api.errors.append(rejection(429, retry_after=5))

        def slow_preparation() -> None:
            self.clock.value += 6  # Rendering and locking took six seconds.

        self.before_send = slow_preparation
        self.assertTrue(self.cycle())
        self.before_send = None
        start = self.now
        self.now = start + timedelta(seconds=9)
        self.clock.value += 9
        self.assertFalse(self.sender.deliver_one(now=self.now))
        self.assertEqual(self.api.requests, 1)
        self.now = start + timedelta(seconds=12)
        self.clock.value += 3
        self.assertTrue(self.sender.deliver_one(now=self.now))

    def test_send_claim_rechecks_the_shown_version_and_the_pause(self) -> None:
        self.open_and_create()
        self.enqueue()
        stale = self.store.due(now=self.now + timedelta(days=1))[0]
        self.assertTrue(self.cycle(seconds=PANEL_IDLE_SECONDS + 1))  # Another sender edits.
        self.assertIsNone(
            self.store.begin(
                stale,
                payload_html="Older snapshot",
                reply_markup={"inline_keyboard": []},
                body_digest="b" * 64,
                now=self.now + timedelta(days=1),
            )
        )
        current = self.store.due(now=self.now + timedelta(days=1))[0]
        self.db.execute(
            "INSERT INTO topic_panel_pause VALUES(1,?,?)",
            ((self.now + timedelta(days=2)).isoformat(), self.now.isoformat()),
        )
        self.db.commit()
        self.assertIsNone(
            self.store.begin(
                current,
                payload_html="Fictional",
                reply_markup={"inline_keyboard": []},
                body_digest="b" * 64,
                now=self.now + timedelta(days=1),
            )
        )
        self.assertEqual(len(self.api.edits), 1)

    def test_unregistered_group_stops_the_panel_without_http(self) -> None:
        self.store.open(self.topic, now=self.now)
        self.registered.clear()
        self.assertFalse(self.cycle())
        self.assertEqual(self.api.requests, 0)
        self.assertEqual(self.panel()["unavailable_reason"], "project_unbound")
        self.assertIn("topic_panel_project_unbound", [event[1] for event in self.events])

    def test_backward_clock_step_never_prunes_the_confirmed_version(self) -> None:
        self.open_and_create()
        earlier = self.now - timedelta(hours=1)
        at = self.now + timedelta(days=1)
        for _ in range(PANEL_SETTLED_RETENTION + 2):
            at += timedelta(seconds=1)
            panel = self.store.due(now=at)[0]
            operation = self.store.begin(
                panel,
                payload_html="Fictional edit",
                reply_markup={"inline_keyboard": []},
                body_digest="a" * 64,
                now=at,
            )
            assert operation is not None
            self.store.reject(
                operation, error_code="not_sent", now=at, retry_at=earlier - timedelta(seconds=1)
            )
        # The wall clock steps back an hour; the next confirmed edit is the oldest row.
        self.enqueue()
        self.clock.value += 2
        self.assertTrue(self.sender.deliver_one(now=earlier))
        self.assertEqual(self.operations()[0]["state"], "confirmed")
        pinned = self.panel()["confirmed_operation_id"]
        self.assertIn(pinned, [row["operation_id"] for row in self.operations()])

    def test_operation_evidence_rejects_null_identity_and_edit_target(self) -> None:
        self.open_and_create()
        row = dict(self.operations()[0])
        for changes in ({"operation_id": None}, {"kind": "edit", "target_message_id": None}):
            candidate = {**row, "operation_id": "c" * 32, "state": "rejected", **changes}
            columns = ",".join(candidate)
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute(
                    f"INSERT INTO topic_panel_operations({columns}) VALUES "
                    f"({','.join('?' for _ in candidate)})",
                    tuple(candidate.values()),
                )
        self.db.rollback()

    def test_permanent_rejection_stops_the_panel_without_touching_health(self) -> None:
        self.open_and_create()
        self.enqueue()
        self.api.errors.append(rejection(400))
        self.assertTrue(self.cycle(seconds=PANEL_IDLE_SECONDS + 1))
        self.assertEqual(self.panel()["unavailable_reason"], "api_400")
        self.assertEqual((self.successes, self.failures), (1, []))
        self.enqueue()
        self.assertFalse(self.cycle(seconds=PANEL_IDLE_SECONDS + 1))
        self.assertEqual(len(self.api.edits), 1)

    def test_not_modified_confirms_only_the_exact_edit(self) -> None:
        self.open_and_create()
        self.enqueue()
        self.api.errors.append(rejection(400, message_not_modified=True))
        self.assertTrue(self.cycle(seconds=PANEL_IDLE_SECONDS + 1))
        operation = self.operations()[-1]
        self.assertEqual((operation["kind"], operation["state"]), ("edit", "confirmed"))
        self.assertEqual(operation["receipt_message_id"], 501)
        self.assertEqual(self.panel()["confirmed_digest"], operation["body_digest"])
        self.assertIsNone(self.panel()["unavailable_reason"])
        self.assertEqual((self.successes, self.failures), (1, []))

    def test_settled_history_is_bounded_and_unknown_evidence_is_kept(self) -> None:
        self.store.open(self.topic, now=self.now)
        self.api.errors.append(
            TelegramError("timeout", operation="send_message", failure_class="network_timeout")
        )
        self.cycle()
        for _ in range(PANEL_SETTLED_RETENTION + 3):
            self.store.open(self.topic, now=self.now)
            self.assertTrue(self.cycle())
        states = [row["state"] for row in self.operations()]
        self.assertEqual(states.count("unknown"), 1)
        self.assertEqual(states.count("confirmed"), PANEL_SETTLED_RETENTION)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("DELETE FROM topic_panel_operations WHERE state='unknown'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "UPDATE topic_panel_operations SET state='confirmed',receipt_message_id=1 "
                "WHERE state='unknown'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE topic_panel_operations SET payload_html='changed'")
        self.db.rollback()

    def test_snapshot_failure_is_isolated_and_postponed(self) -> None:
        self.store.open(self.topic, now=self.now)
        with patch(
            "hermes_codex_router.topic_panel_sender.read_panel_snapshot",
            side_effect=sqlite3.OperationalError("fictional"),
        ):
            self.assertFalse(self.cycle())
        self.assertFalse(self.db.in_transaction)
        self.assertEqual(self.api.requests, 0)
        self.assertFalse(self.cycle())
        self.assertTrue(self.cycle(seconds=PANEL_IDLE_SECONDS))
        self.assertEqual(self.api.requests, 1)

    def test_scan_runs_at_most_once_per_second(self) -> None:
        self.store.open(self.topic, now=self.now)
        self.assertTrue(self.cycle())
        self.store.open(self.topic, now=self.now)
        self.assertFalse(self.cycle(seconds=0.1))
        self.assertTrue(self.cycle(seconds=1.0))
        self.assertEqual(len(self.api.creates), 2)

    def test_panel_rows_never_touch_jobs_or_outbox(self) -> None:
        self.enqueue()
        before = (
            self.db.execute("SELECT COUNT(*) FROM provider_jobs").fetchone()[0],
            self.db.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0],
        )
        self.open_and_create()
        self.store.close(self.topic.topic_id, now=self.now)
        after = (
            self.db.execute("SELECT COUNT(*) FROM provider_jobs").fetchone()[0],
            self.db.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0],
        )
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
