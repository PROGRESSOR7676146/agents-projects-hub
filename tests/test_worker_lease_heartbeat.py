from __future__ import annotations

import sqlite3
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, cast
from unittest.mock import patch

from hermes_codex_router.external_worker import ExternalQueueWorker
from hermes_codex_router.provider_lease_heartbeat import maintain_provider_lease
from hermes_codex_router.state import HubState, StateError
from tests import test_external_worker as fixture_module


class BoundedStop:
    def __init__(self, on_wait: Callable[[float], None], *, limit: int = 2) -> None:
        self.on_wait = on_wait
        self.limit = limit
        self.waits: list[float] = []

    def is_set(self) -> bool:
        return len(self.waits) >= self.limit

    def wait(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.on_wait(seconds)
        return self.is_set()


class WorkerLeaseHeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixture_module.ExternalQueueWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.job_id = self.fixture.enqueue("opencode", 1)
        self.adapter = fixture_module.Adapter("opencode")
        self.worker = ExternalQueueWorker(
            self.fixture.config,
            "opencode",
            registry=self.fixture.registry,
            adapter=cast(Any, self.adapter),
        )
        self.addCleanup(self.worker.close)
        leased = self.worker.state.lease_provider_job("opencode", self.worker.worker_id)
        assert leased is not None and leased.lease_token is not None
        self.token = leased.lease_token
        self.job = self.worker.state.mark_provider_job_executing(self.job_id, self.token)
        self.writer = sqlite3.connect(self.fixture.config.state_path, timeout=0.01)
        self.addCleanup(self.writer.close)

    def maintain(self, stop: BoundedStop) -> None:
        self.worker._maintain_provider_lease(self.job, self.token, cast(threading.Event, stop))

    def test_real_writer_contention_retries_without_losing_task_or_root(self) -> None:
        before = self.worker.state.get_provider_job(self.job_id)
        self.writer.execute("BEGIN IMMEDIATE")
        stop = BoundedStop(lambda _: self.writer.rollback())
        with patch.object(HubState, "open", side_effect=AssertionError("no migration")):
            self.maintain(stop)
        after = self.worker.state.get_provider_job(self.job_id)
        self.assertEqual(stop.waits, [1, 30])
        self.assertEqual(after.status, "executing")
        self.assertEqual(after.lease_token, self.token)
        assert after.lease_expires_at is not None and before.lease_expires_at is not None
        self.assertGreater(after.lease_expires_at, before.lease_expires_at)
        self.assertEqual(after.attempt_count, before.attempt_count)
        self.assertEqual(self.adapter.calls, 0)
        self.assertIsNone(self.worker.state.lease_provider_job("opencode", "other-worker"))

    def test_retry_cannot_revive_a_lease_that_expired_during_contention(self) -> None:
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.writer.execute("BEGIN IMMEDIATE")

        def expire_after_release(_: float) -> None:
            self.writer.execute(
                "UPDATE provider_jobs SET lease_expires_at=? WHERE job_id=?",
                (expired, self.job_id),
            )
            self.writer.commit()

        stop = BoundedStop(expire_after_release)
        self.maintain(stop)
        after = self.worker.state.get_provider_job(self.job_id)
        self.assertEqual(stop.waits, [1])
        self.assertEqual(after.lease_expires_at, expired)
        self.assertEqual(after.status, "executing")
        self.assertEqual(self.adapter.calls, 0)

    def test_expiry_while_acquiring_writer_is_checked_after_acquisition(self) -> None:
        state = self.worker.state
        before = datetime.now(timezone.utc)
        expiry = (before + timedelta(seconds=1)).isoformat()
        with state._connection:
            state._connection.execute(
                "UPDATE provider_jobs SET lease_expires_at=? WHERE job_id=?",
                (expiry, self.job_id),
            )
        clock = [before]
        original = state._provider_job_state._write_transaction

        @contextmanager
        def lock_acquired_later():
            with original():
                clock[0] = before + timedelta(seconds=2)
                yield

        class LockClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0]

        with (
            patch.object(state._provider_job_state, "_write_transaction", lock_acquired_later),
            patch("hermes_codex_router.state_provider_jobs.datetime", LockClock),
            self.assertRaises(StateError),
        ):
            state.heartbeat_provider_job(self.job_id, self.token, lease_seconds=120)
        self.assertEqual(state.get_provider_job(self.job_id).lease_expires_at, expiry)
        self.assertEqual(state.get_provider_job(self.job_id).attempt_count, 1)
        self.assertEqual(self.adapter.calls, 0)

    def test_busy_open_is_retried_but_no_database_is_created_or_migrated(self) -> None:
        self.writer.execute("BEGIN IMMEDIATE")
        contender = sqlite3.connect(self.fixture.config.state_path, timeout=0.001)
        try:
            try:
                contender.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                busy = error
            else:
                self.fail("fixture must produce actual SQLite contention")
        finally:
            contender.close()
            self.writer.rollback()
        original = HubState.open_existing
        calls = 0

        def opening(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise busy
            return original(*args, **kwargs)

        stop = BoundedStop(lambda _: None)
        with patch.object(HubState, "open_existing", side_effect=opening):
            self.maintain(stop)
        self.assertEqual(stop.waits, [1, 30])
        self.assertEqual(calls, 2)

    def test_same_text_without_sqlite_lock_code_does_not_authorize_retry(self) -> None:
        stop = BoundedStop(lambda _: None)
        with patch.object(
            HubState,
            "heartbeat_provider_job",
            side_effect=sqlite3.OperationalError("database is locked"),
        ) as heartbeat:
            self.maintain(stop)
        self.assertEqual(heartbeat.call_count, 1)
        self.assertEqual(stop.waits, [])

    def test_changed_token_stops_without_reclaiming_or_executing(self) -> None:
        with self.worker.state._immediate_transaction():
            self.worker.state._connection.execute(
                "UPDATE provider_jobs SET lease_token='replacement-token' WHERE job_id=?",
                (self.job_id,),
            )
        stop = BoundedStop(lambda _: None)
        self.maintain(stop)
        self.assertEqual(stop.waits, [])
        self.assertEqual(
            self.worker.state.get_provider_job(self.job_id).lease_token, "replacement-token"
        )
        self.assertEqual(self.adapter.calls, 0)

    def test_embedded_path_uses_the_same_guard_without_health_callback(self) -> None:
        self.writer.execute("BEGIN IMMEDIATE")
        stop = BoundedStop(lambda _: self.writer.rollback())
        maintain_provider_lease(
            self.fixture.config, self.job_id, self.token, cast(threading.Event, stop)
        )
        self.assertEqual(stop.waits, [1, 30])
        self.assertEqual(self.worker.state.get_provider_job(self.job_id).status, "executing")
        self.assertEqual(self.adapter.calls, 0)

    def test_missing_database_stays_missing_including_failure_reporting(self) -> None:
        missing = self.fixture.config.state_path.with_name("missing-state.db")
        self.worker.config = replace(self.worker.config, state_path=missing)
        stop = BoundedStop(lambda _: None)
        self.maintain(stop)
        self.assertFalse(missing.exists())
        self.assertEqual(stop.waits, [])


if __name__ == "__main__":
    unittest.main()
