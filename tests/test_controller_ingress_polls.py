"""Actual poll seam, fenced instances and isolated diagnostic failures."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.controller_ingress_polls import ControllerIngressPolls
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.state import HubState
from hermes_codex_router.telegram import TelegramError


class ControllerIngressPollTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = HubState.open(
            Path(temporary.name) / "example-state.db", codex_permission_profile=None
        )
        self.addCleanup(self.state.close)
        self.now = datetime.now(timezone.utc)

    def service(self, identity="hub", direct=False):
        service = cast(Any, object.__new__(ProjectHubService))
        service.state = self.state
        service.agent = SimpleNamespace(agent_id="codex")
        service.config = SimpleNamespace()
        service.supervisor = None
        service.ingress_identity = identity
        service.direct_messages_only = direct
        service._publishes_controller_health = not direct
        service._stop = threading.Event()
        return service

    def run_empty_poll(self, service):
        def empty_updates(**kwargs):
            service._stop.set()
            return []

        service.telegram = SimpleNamespace(updates=empty_updates)
        with (
            patch.object(service, "_publish_runtime_health"),
            patch.object(service, "_start_embedded_queue_consumer"),
            patch.object(service, "_start_controller_outbox_delivery"),
        ):
            service.run_forever()

    def test_actual_empty_group_poll_registers_selected_ingress_only(self):
        service = self.service()
        self.run_empty_poll(service)
        current = self.state.telegram_ingress.read("hub")
        assert current is not None
        self.assertIsNotNone(current.evidence.last_success_at)
        self.assertIsNone(self.state.telegram_ingress.read("codex"))
        service._stop.clear()
        self.run_empty_poll(service)
        self.assertEqual(self.state.telegram_ingress.current_epoch("hub"), 1)

    def test_compatibility_group_codex_and_direct_provider_endpoints_are_distinct(self):
        self.run_empty_poll(self.service(identity="codex", direct=True))
        self.assertIsNone(self.state.telegram_ingress.read("codex"))
        self.run_empty_poll(self.service(identity="codex"))
        current = self.state.telegram_ingress.read("codex")
        assert current is not None
        self.assertIsNotNone(current.evidence.last_success_at)

    def test_unsupported_group_identity_keeps_polling_without_an_ingress_collector(self):
        service = self.service(identity="opencode")
        service.agent = SimpleNamespace(agent_id="opencode")
        self.run_empty_poll(service)
        self.assertIsNone(service._group_ingress_polls)
        self.assertIsNone(self.state.telegram_ingress.read("hub"))
        self.assertIsNone(self.state.telegram_ingress.read("codex"))

    def test_success_commits_before_optional_recovery_diagnostic_failure(self):
        service = self.service()
        service._group_ingress_polls = ControllerIngressPolls(self.state.telegram_ingress, "hub")
        service._health_transport_reported_signature = ("poll", "network_timeout", None)
        with patch.object(
            self.state, "record_runtime_event", side_effect=sqlite3.OperationalError("example")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                service._record_telegram_poll_success("hub")
        current = self.state.telegram_ingress.read("hub")
        assert current is not None
        self.assertIsNotNone(current.evidence.last_success_at)

    def test_failure_commits_before_threshold_diagnostic_failure_without_raw_error(self):
        service = self.service()
        service._group_ingress_polls = ControllerIngressPolls(self.state.telegram_ingress, "hub")
        error = TelegramError("example", operation="poll", failure_class="network_timeout")
        for _ in range(2):
            service._record_telegram_poll_failure("hub", error)
        with patch.object(
            self.state, "record_runtime_event", side_effect=sqlite3.OperationalError("example")
        ):
            with self.assertRaises(sqlite3.OperationalError):
                service._record_telegram_poll_failure("hub", error)
        current = self.state.telegram_ingress.read("hub")
        assert current is not None
        self.assertEqual(current.evidence.failure_streak, 3)
        self.assertIsNotNone(current.evidence.failure_threshold_at)
        self.assertNotIn(
            "network_timeout",
            repr(
                tuple(
                    self.state._connection.execute(
                        "SELECT * FROM telegram_group_ingress"
                    ).fetchone()
                )
            ),
        )

    def test_poll_commit_contention_allows_next_sample_without_false_consecutiveness(self):
        polls = ControllerIngressPolls(self.state.telegram_ingress, "hub")
        polls.record(succeeded=False, observed_at=self.now + timedelta(seconds=1))
        with patch.object(
            self.state.telegram_ingress,
            "record_poll",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            polls.record(succeeded=False, observed_at=self.now + timedelta(seconds=2))
        polls.record(succeeded=False, observed_at=self.now + timedelta(seconds=3))
        current = self.state.telegram_ingress.read("hub")
        assert current is not None
        self.assertEqual(current.evidence.failure_streak, 1)
        self.assertIsNone(current.evidence.failure_threshold_at)

    def test_stale_publisher_retires_and_never_calls_register_again(self):
        old = ControllerIngressPolls(self.state.telegram_ingress, "hub")
        latest = ControllerIngressPolls(self.state.telegram_ingress, "hub")
        with patch.object(
            self.state.telegram_ingress, "register", side_effect=AssertionError("no reacquisition")
        ):
            old.record(succeeded=True, observed_at=self.now + timedelta(seconds=1))
            old.record(succeeded=True, observed_at=self.now + timedelta(seconds=2))
            latest.record(succeeded=False, observed_at=self.now + timedelta(seconds=3))
        current = self.state.telegram_ingress.read("hub")
        assert current is not None
        self.assertEqual(current.evidence.epoch, 2)
        self.assertIsNone(current.last_confirmed_poll_at)
        self.assertEqual(current.evidence.failure_streak, 1)

    def test_ambiguous_sample_commit_preserves_one_count_and_next_sequence(self):
        polls = ControllerIngressPolls(self.state.telegram_ingress, "hub")
        original = self.state.telegram_ingress.record_poll

        def commit_then_lose_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("example outcome lost after commit")

        with patch.object(
            self.state.telegram_ingress, "record_poll", side_effect=commit_then_lose_ack
        ):
            polls.record(succeeded=False, observed_at=self.now + timedelta(seconds=1))
        polls.record(succeeded=False, observed_at=self.now + timedelta(seconds=2))
        current = self.state.telegram_ingress.read("hub")
        assert current is not None
        self.assertEqual(current.evidence.failure_streak, 2)
        self.assertIsNone(current.evidence.failure_threshold_at)

    def test_ambiguous_registration_retries_original_intent_without_new_epoch(self):
        original = self.state.telegram_ingress.register

        def commit_then_lose_ack(*args, **kwargs):
            original(*args, **kwargs)
            raise sqlite3.OperationalError("example outcome lost after commit")

        with patch.object(
            self.state.telegram_ingress, "register", side_effect=commit_then_lose_ack
        ):
            polls = ControllerIngressPolls(self.state.telegram_ingress, "hub")
        with patch.object(self.state.telegram_ingress, "register", wraps=original) as retried:
            polls.record(succeeded=True, observed_at=self.now + timedelta(seconds=1))
        self.assertEqual(retried.call_args.kwargs["instance_token"], polls.instance_token)
        self.assertEqual(retried.call_args.kwargs["previous_epoch"], 0)
        current = self.state.telegram_ingress.read("hub")
        assert current is not None
        self.assertEqual(current.evidence.epoch, 1)
        self.assertIsNotNone(current.evidence.last_poll_at)

    def test_startup_read_or_write_contention_recovers_from_same_startup_intent(self):
        for method in ("current_epoch", "register"):
            with self.subTest(method=method):
                with patch.object(
                    self.state.telegram_ingress,
                    method,
                    side_effect=sqlite3.OperationalError("database is locked"),
                ):
                    polls = ControllerIngressPolls(self.state.telegram_ingress, "hub")
                token = polls.instance_token
                observed_at = datetime.now(timezone.utc)
                with patch("hermes_codex_router.controller_ingress_polls.datetime") as clock:
                    clock.now.return_value = observed_at + timedelta(seconds=1)
                    polls.record(succeeded=True, observed_at=observed_at)
                self.assertIsNotNone(polls.owner)
                self.assertEqual(polls.instance_token, token)
                current = self.state.telegram_ingress.read("hub")
                assert current is not None
                self.assertEqual(current.evidence.last_poll_at, observed_at)
                self.assertEqual(current.evidence.last_success_at, observed_at)
                self.assertEqual(
                    self.state._connection.execute(
                        "SELECT poll_sequence FROM telegram_group_ingress WHERE identity='hub'"
                    ).fetchone()[0],
                    1,
                )

    def test_first_successful_epoch_snapshot_defines_initial_startup_claim(self):
        with patch.object(
            self.state.telegram_ingress,
            "current_epoch",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            earlier = ControllerIngressPolls(self.state.telegram_ingress, "hub")
        latest = ControllerIngressPolls(self.state.telegram_ingress, "hub")
        earlier.record(succeeded=True, observed_at=self.now + timedelta(seconds=1))
        self.assertFalse(earlier.registration_pending)
        self.assertIsNotNone(earlier.owner)
        self.assertEqual(earlier.previous_epoch, 1)
        latest.record(succeeded=True, observed_at=self.now + timedelta(seconds=2))
        self.assertIsNone(latest.owner)
        self.assertEqual(self.state.telegram_ingress.current_epoch("hub"), 2)

    def test_captured_pending_cas_cannot_reclaim_after_competing_startup(self):
        with patch.object(
            self.state.telegram_ingress,
            "register",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            earlier = ControllerIngressPolls(self.state.telegram_ingress, "hub")
        self.assertEqual(earlier.previous_epoch, 0)
        latest = ControllerIngressPolls(self.state.telegram_ingress, "hub")
        earlier.record(succeeded=True, observed_at=self.now + timedelta(seconds=1))
        self.assertFalse(earlier.registration_pending)
        self.assertIsNone(earlier.owner)
        self.assertIsNotNone(latest.owner)
        self.assertEqual(self.state.telegram_ingress.current_epoch("hub"), 1)

    def test_backward_startup_clock_defers_original_cas_until_clock_catches_up(self):
        old = self.state.telegram_ingress.register(
            "hub", instance_token="example-old-instance", previous_epoch=0, now=self.now
        )
        self.state.telegram_ingress.record_poll(
            old, sequence=1, succeeded=True, observed_at=self.now + timedelta(seconds=10)
        )
        with patch("hermes_codex_router.controller_ingress_polls.datetime") as clock:
            clock.now.return_value = self.now + timedelta(seconds=5)
            polls = ControllerIngressPolls(self.state.telegram_ingress, "hub")
            self.assertTrue(polls.registration_pending)
            self.assertEqual(polls.previous_epoch, 1)
            token = polls.instance_token
            clock.now.return_value = self.now + timedelta(seconds=13)
            polls.record(succeeded=True, observed_at=self.now + timedelta(seconds=12))
        self.assertIsNotNone(polls.owner)
        self.assertFalse(polls.registration_pending)
        self.assertEqual((polls.previous_epoch, polls.instance_token), (1, token))
        self.assertEqual(self.state.telegram_ingress.current_epoch("hub"), 2)
        current = self.state.telegram_ingress.read("hub")
        assert current is not None
        self.assertEqual(current.evidence.last_poll_at, self.now + timedelta(seconds=12))
        self.assertEqual(current.last_confirmed_poll_at, self.now + timedelta(seconds=12))

    def test_real_nested_transaction_guard_skips_sample_and_recovers(self):
        polls = ControllerIngressPolls(self.state.telegram_ingress, "hub")
        owner = polls.owner
        with self.state._immediate_transaction():
            polls.record(succeeded=False, observed_at=self.now + timedelta(seconds=1))
        self.assertEqual(polls.owner, owner)
        polls.record(succeeded=False, observed_at=self.now + timedelta(seconds=2))
        current = self.state.telegram_ingress.read("hub")
        assert current is not None
        self.assertEqual(current.evidence.failure_streak, 1)

    def test_backward_clock_sample_is_skipped_without_retiring_owner(self):
        polls = ControllerIngressPolls(self.state.telegram_ingress, "hub")
        polls.record(succeeded=False, observed_at=self.now + timedelta(seconds=5))
        owner = polls.owner
        polls.record(succeeded=False, observed_at=self.now + timedelta(seconds=4))
        self.assertEqual(polls.owner, owner)
        polls.record(succeeded=False, observed_at=self.now + timedelta(seconds=6))
        current = self.state.telegram_ingress.read("hub")
        assert current is not None
        self.assertEqual(current.evidence.failure_streak, 1)
        polls.record(succeeded=True, observed_at=self.now + timedelta(seconds=7))
        restored = self.state.telegram_ingress.read("hub")
        assert restored is not None
        self.assertEqual(restored.evidence.failure_streak, 0)


if __name__ == "__main__":
    unittest.main()
