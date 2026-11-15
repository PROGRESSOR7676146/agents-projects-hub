"""Reproduce the cold-boot recovery ordering with isolated fictional state."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import codex_server_service as service
from hermes_codex_router.state import HubState
from tests import test_codex_turn_controls as fixtures
from tests import test_codex_worker as worker_fixtures


class CodexBootRecoveryRaceTests(unittest.TestCase):
    def test_database_write_lock_temporarily_blocks_new_lease_without_losing_queue(self):
        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.addCleanup(fixture.doCleanups)
        job_id = fixture.enqueue()
        state = HubState.open(fixture.config.state_path, codex_permission_profile=None)
        self.addCleanup(state.close)
        state._connection.execute("PRAGMA busy_timeout=100")
        barrier = sqlite3.connect(fixture.config.state_path, timeout=0.1)
        self.addCleanup(barrier.close)
        barrier.execute("BEGIN IMMEDIATE")
        with self.assertRaises(sqlite3.OperationalError):
            state.lease_provider_job("codex", "example-worker")
        self.assertEqual(state.get_provider_job(job_id).status, "queued")
        barrier.rollback()
        claimed = state.lease_provider_job("codex", "example-worker")
        assert claimed is not None
        self.assertEqual(claimed.job_id, job_id)

    def test_idle_snapshot_does_not_fence_a_later_worker_claim(self):
        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.addCleanup(fixture.doCleanups)
        state = HubState.open(fixture.config.state_path, codex_permission_profile=None)
        self.addCleanup(state.close)
        service.require_idle_state(fixture.config.state_path)
        job_id = fixture.enqueue()
        claimed = state.lease_provider_job("codex", "example-worker")
        assert claimed is not None
        self.assertEqual(claimed.job_id, job_id)
        self.assertEqual(claimed.status, "leased")

    def test_recovering_interrupted_execution_first_still_leaves_queued_work_blocking_boot(self):
        fixture = fixtures.CodexTurnControlJournalTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        queued_job = fixture.fixture.enqueue(message_id=2)
        fixture.state.recover_stale_provider_jobs(
            agent_id="codex", now=datetime.now(timezone.utc) + timedelta(days=1)
        )
        before = fixture.state.get_provider_job(queued_job)
        self.assertEqual(before.status, "queued")
        manifest = {
            "state_path": str(fixture.fixture.config.state_path),
            "codex_home": str(fixture.root),
        }
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            with (
                patch.object(service, "boot_identity", return_value=("example-boot", 1.0)),
                patch.object(service, "require_lingering"),
                patch.object(service, "refuse_native_clients"),
                patch.object(service, "refuse_existing_servers"),
                patch.object(service, "verify_manifest", return_value=runtime),
                patch.object(service, "remove_previous_boot_link"),
                patch.object(service, "verify_pins"),
            ):
                with self.assertRaisesRegex(ValueError, "queued job"):
                    service.authorize_start(manifest, None, runtime)
                with self.assertRaisesRegex(ValueError, "fresh start receipt"):
                    service.authorize_start(manifest, None, runtime)
        self.assertEqual(fixture.state.get_provider_job(queued_job), before)

    def test_refusal_is_spent_before_stale_execution_and_queue_are_recovered(self):
        fixture = fixtures.CodexTurnControlJournalTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        queued_job = fixture.fixture.enqueue(message_id=2)
        manifest = {
            "state_path": str(fixture.fixture.config.state_path),
            "codex_home": str(fixture.root),
        }
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            with (
                patch.object(service, "boot_identity", return_value=("example-boot", 1.0)),
                patch.object(service, "require_lingering"),
                patch.object(service, "refuse_native_clients"),
                patch.object(service, "refuse_existing_servers"),
                patch.object(service, "verify_manifest", return_value=runtime),
                patch.object(service, "remove_previous_boot_link"),
                patch.object(service, "verify_pins"),
            ):
                with self.assertRaisesRegex(ValueError, "active or unknown Codex work"):
                    service.authorize_start(manifest, None, runtime)

                recovery = fixture.state.recover_stale_provider_jobs(
                    agent_id="codex", now=datetime.now(timezone.utc) + timedelta(days=1)
                )
                self.assertIn(fixture.job_id, recovery.indeterminate_job_ids)
                self.assertEqual(fixture.state.get_provider_job(queued_job).status, "queued")
                with self.assertRaisesRegex(ValueError, "queued job"):
                    service.require_idle_state(fixture.fixture.config.state_path)
                with self.assertRaisesRegex(ValueError, "fresh start receipt"):
                    service.authorize_start(manifest, None, runtime)


if __name__ == "__main__":
    unittest.main()
