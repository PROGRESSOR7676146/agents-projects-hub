from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.state import HubState, StateError


class RuntimeHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state = HubState.open(Path(self.tempdir.name) / "state.db")
        self.now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def test_health_snapshot_round_trips_bounded_non_secret_fields(self) -> None:
        record = self.state.upsert_runtime_health(
            component="provider_worker",
            instance_id="worker-opencode-1",
            runtime="opencode",
            agent_id="opencode",
            pid=1234,
            process_start_marker="linux-proc-start-42",
            started_at=self.now - timedelta(minutes=2),
            heartbeat_at=self.now,
            success_at=self.now - timedelta(seconds=5),
            error_code=None,
            active_job_id="job-example-1",
            active_lease_expires_at=self.now + timedelta(minutes=2),
            provider_state="ready",
            quota_remaining_percent=75.5,
            quota_reset_at=self.now + timedelta(hours=1),
        )

        self.assertEqual(record.component, "provider_worker")
        self.assertEqual(record.instance_id, "worker-opencode-1")
        self.assertEqual(record.runtime, "opencode")
        self.assertEqual(record.agent_id, "opencode")
        self.assertEqual(record.pid, 1234)
        self.assertEqual(record.process_start_marker, "linux-proc-start-42")
        self.assertEqual(record.active_job_id, "job-example-1")
        self.assertEqual(record.provider_state, "ready")
        self.assertEqual(record.quota_remaining_percent, 75.5)
        self.assertEqual(
            self.state.get_runtime_health("provider_worker", "worker-opencode-1"),
            record,
        )

    def test_upsert_replaces_ephemeral_state_but_preserves_started_at(self) -> None:
        started = self.now - timedelta(minutes=2)
        self.state.upsert_runtime_health(
            component="sender",
            instance_id="sender-1",
            runtime="telegram",
            agent_id="opencode",
            pid=2345,
            process_start_marker="start-1",
            started_at=started,
            heartbeat_at=self.now - timedelta(seconds=5),
            error_code="telegram_timeout",
            active_job_id="outbox-example-1",
        )

        refreshed = self.state.upsert_runtime_health(
            component="sender",
            instance_id="sender-1",
            runtime="telegram",
            agent_id="opencode",
            pid=2345,
            process_start_marker="start-1",
            started_at=self.now,
            heartbeat_at=self.now,
            success_at=self.now,
            error_code=None,
            active_job_id=None,
        )

        self.assertEqual(refreshed.started_at, started.isoformat())
        self.assertIsNone(refreshed.error_code)
        self.assertIsNone(refreshed.active_job_id)
        self.assertEqual(refreshed.success_at, self.now.isoformat())

    def test_health_classification_uses_cached_timestamps_only(self) -> None:
        self.state.upsert_runtime_health(
            component="controller",
            instance_id="controller-healthy",
            pid=3456,
            process_start_marker="start-healthy",
            started_at=self.now - timedelta(minutes=5),
            heartbeat_at=self.now - timedelta(seconds=10),
            success_at=self.now - timedelta(seconds=20),
        )
        self.state.upsert_runtime_health(
            component="provider_worker",
            instance_id="worker-limited",
            runtime="codex",
            agent_id="codex",
            pid=4567,
            process_start_marker="start-limited",
            started_at=self.now - timedelta(minutes=5),
            heartbeat_at=self.now - timedelta(seconds=10),
            error_code="provider_limit",
            provider_state="limited",
            quota_remaining_percent=0,
        )
        self.state.upsert_runtime_health(
            component="sender",
            instance_id="sender-degraded",
            pid=5678,
            process_start_marker="start-degraded",
            started_at=self.now - timedelta(minutes=5),
            heartbeat_at=self.now - timedelta(seconds=90),
        )
        self.state.upsert_runtime_health(
            component="provider_worker",
            instance_id="worker-stale",
            runtime="antigravity",
            agent_id="antigravity",
            pid=6789,
            process_start_marker="start-stale",
            started_at=self.now - timedelta(minutes=10),
            heartbeat_at=self.now - timedelta(minutes=4),
        )

        def classify(component: str, instance: str) -> str:
            return self.state.runtime_health_status(
                component,
                instance,
                now=self.now,
                degraded_after=timedelta(seconds=60),
                stale_after=timedelta(minutes=3),
            ).status

        self.assertEqual(classify("controller", "controller-healthy"), "healthy")
        self.assertEqual(classify("provider_worker", "worker-limited"), "degraded")
        self.assertEqual(classify("sender", "sender-degraded"), "degraded")
        self.assertEqual(classify("provider_worker", "worker-stale"), "stale")
        self.assertEqual(classify("sender", "missing"), "unknown")

    def test_invalid_or_oversized_health_fields_fail_closed(self) -> None:
        common = dict(
            component="controller",
            instance_id="controller-1",
            pid=1234,
            process_start_marker="start-1",
            started_at=self.now,
            heartbeat_at=self.now,
        )
        with self.assertRaises(StateError):
            self.state.upsert_runtime_health(**cast(Any, common | {"error_code": "x" * 129}))
        with self.assertRaises(StateError):
            self.state.upsert_runtime_health(**cast(Any, common | {"quota_remaining_percent": 101}))
        with self.assertRaises(StateError):
            self.state.upsert_runtime_health(
                **cast(Any, common | {"provider_state": "secret-bearing-detail"})
            )
        with self.assertRaises(StateError):
            self.state.upsert_runtime_health(**cast(Any, common | {"pid": 0}))


if __name__ == "__main__":
    unittest.main()
