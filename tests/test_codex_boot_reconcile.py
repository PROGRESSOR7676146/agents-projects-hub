"""ADR 0104: the first boot start settles interrupted Codex executions first."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import codex_boot_reconcile as reconcile
from hermes_codex_router import codex_server_service as service
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.state import HubState, StateError
from tests import test_codex_turn_controls as fixtures


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(seconds, 0.5)


class CodexBootReconcileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.journal_fixture = fixtures.CodexTurnControlJournalTests()
        self.journal_fixture.setUp()
        self.addCleanup(self.journal_fixture.doCleanups)
        self.worker = self.journal_fixture.fixture
        self.state: HubState = self.journal_fixture.state
        self.job_id = self.journal_fixture.job_id
        self.token = self.journal_fixture.token
        self.manifest = {
            "state_path": str(self.worker.config.state_path),
            "codex_home": str(self.journal_fixture.root),
            "codex_binary": {"path": "/example/pinned/codex"},
        }
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.runtime = Path(directory.name)
        self.hub_config = self.runtime / "hub.json"
        environment = patch.dict("os.environ", {})
        environment.start()
        self.addCleanup(environment.stop)

    def expire_lease(self) -> None:
        self.state.heartbeat_provider_job(
            self.job_id,
            self.token,
            lease_seconds=1,
            now=datetime.now(timezone.utc) - timedelta(seconds=10),
        )

    def keep_lease(self) -> None:
        self.state.heartbeat_provider_job(self.job_id, self.token, lease_seconds=3600)

    def complete_checkpoint(self) -> None:
        journal = ExecutionJournal(self.state)
        journal.record_turn(self.job_id, self.token, "example-turn")
        journal.record_completion(self.job_id, self.token, "Fictional saved result")

    def reconcile(self, clock: FakeClock | None = None, factory=None, **options) -> int:
        clock = clock or FakeClock()
        with patch.object(reconcile, "load_hub_config", return_value=self.worker.config):
            return reconcile.reconcile_previous_boot(
                self.manifest,
                self.hub_config,
                verified_binary=Path("/example/pinned/codex"),
                client_factory=factory
                or (lambda: self.fail("a saved completion needs no provider")),
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                **options,
            )

    def first_boot_start(
        self, *, receipt: Path | None = None, consumer: str | None = None, **reconcile_patch
    ):
        with (
            patch.object(service, "codex_consumer_running", return_value=consumer) as scan,
            patch.object(service, "boot_identity", return_value=("example-boot", 1.0)),
            patch.object(service, "require_lingering"),
            patch.object(service, "refuse_native_clients") as clients,
            patch.object(service, "refuse_existing_servers"),
            patch.object(service, "verify_manifest", return_value=self.runtime / "absent.sock"),
            patch.object(service, "remove_previous_boot_link"),
            patch.object(service, "verify_pins"),
            patch.object(reconcile, "load_hub_config", return_value=self.worker.config),
            patch.object(reconcile, "stdio_client_factory", **reconcile_patch),
        ):
            try:
                service.authorize_start(self.manifest, receipt, self.runtime, self.hub_config)
            finally:
                self.client_scans = clients.call_count
                self.consumer_scans = scan.call_count

    def test_interrupted_execution_is_settled_and_the_first_start_passes_the_gate(self):
        self.complete_checkpoint()
        self.expire_lease()
        with self.assertRaisesRegex(ValueError, "executing job"):
            service.require_idle_state(self.worker.config.state_path)

        self.first_boot_start(return_value=lambda: self.fail("no provider access"))

        self.assertEqual(self.state.get_provider_job(self.job_id).status, "result_ready")
        # The client and server refusal is repeated after the temporary client.
        self.assertEqual(self.client_scans, 2)

    def test_unconfirmed_execution_becomes_indeterminate_without_replay(self):
        self.expire_lease()
        calls: list[str] = []

        def unavailable():
            calls.append("client")
            raise OSError("fictional stdio start failure")

        self.assertEqual(self.reconcile(factory=unavailable), 1)
        job = self.state.get_provider_job(self.job_id)
        self.assertEqual((job.status, job.error_code), ("indeterminate", "recovery_unconfirmed"))
        self.assertEqual(job.attempt_count, 1)
        service.require_idle_state(self.worker.config.state_path)

    def test_a_lease_that_stays_valid_is_left_in_place_within_the_deadline(self):
        self.complete_checkpoint()
        self.keep_lease()
        clock = FakeClock()

        self.assertEqual(self.reconcile(clock), 0)

        self.assertEqual(self.state.get_provider_job(self.job_id).status, "executing")
        self.assertTrue(clock.sleeps)
        self.assertLessEqual(clock.now, reconcile.DEFAULT_DEADLINE_SECONDS + 1)
        with self.assertRaisesRegex(ValueError, "executing job"):
            service.require_idle_state(self.worker.config.state_path)

    def test_a_lease_that_expires_during_the_wait_is_settled(self):
        self.complete_checkpoint()
        self.keep_lease()
        clock = FakeClock()
        sleep = clock.sleep

        def expire_then_sleep(seconds: float) -> None:
            sleep(seconds)
            self.expire_lease()

        clock.sleep = expire_then_sleep  # type: ignore[method-assign]
        self.assertEqual(self.reconcile(clock), 1)
        self.assertEqual(self.state.get_provider_job(self.job_id).status, "result_ready")

    def queued_tail(self) -> tuple[str, str]:
        queued = self.worker.enqueue(message_id=2)
        waiting = self.worker.enqueue(message_id=3)
        # Fictional state shape: retry_wait is entered only from a pre-execution lease.
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE provider_jobs SET status='retry_wait', next_attempt_at=created_at "
                "WHERE job_id=?",
                (waiting,),
            )
        return queued, waiting

    def test_queued_work_is_untouched_and_still_blocks_while_a_consumer_runs(self):
        queued = self.worker.enqueue(message_id=2)
        before = self.state.get_provider_job(queued)
        self.complete_checkpoint()
        self.expire_lease()

        with self.assertRaisesRegex(ValueError, "queued job"):
            self.first_boot_start(
                consumer="4242", return_value=lambda: self.fail("no provider access")
            )

        self.assertEqual(self.state.get_provider_job(self.job_id).status, "result_ready")
        self.assertEqual(self.state.get_provider_job(queued), before)
        self.assertEqual(self.consumer_scans, 1)

    def test_queued_tail_does_not_block_when_no_consumer_runs(self):
        queued, waiting = self.queued_tail()
        before = [self.state.get_provider_job(job) for job in (queued, waiting)]
        self.complete_checkpoint()
        self.expire_lease()

        self.first_boot_start(return_value=lambda: self.fail("no provider access"))

        self.assertEqual(self.state.get_provider_job(self.job_id).status, "result_ready")
        self.assertEqual([self.state.get_provider_job(job) for job in (queued, waiting)], before)
        self.assertEqual((self.client_scans, self.consumer_scans), (2, 1))

    def test_started_or_unsettled_work_still_blocks_with_no_consumer(self):
        self.queued_tail()
        self.keep_lease()
        with (
            patch.object(reconcile, "DEFAULT_DEADLINE_SECONDS", 0.0),
            self.assertRaisesRegex(ValueError, "executing job"),
        ):
            self.first_boot_start(return_value=lambda: self.fail("no provider access"))
        path = self.worker.config.state_path
        # A lease, even an expired one, belongs to ordinary stale recovery.
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE provider_jobs SET status='leased', lease_expires_at='2000-01-01T00:00:00Z' "
                "WHERE job_id=?",
                (self.job_id,),
            )
        with self.assertRaisesRegex(ValueError, "leased job"):
            service.require_idle_state(path, allow_queued_tail=True)
        job = self.state.get_provider_job(self.job_id)
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE provider_jobs SET status='cancelled', lease_owner=NULL, lease_token=NULL, "
                "lease_expires_at=NULL WHERE job_id=?",
                (self.job_id,),
            )
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
                (job.session_id,),
            )
        with self.assertRaisesRegex(ValueError, "native writer"):
            service.require_idle_state(path, allow_queued_tail=True)
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE agent_sessions SET writer_mode='telegram' WHERE session_id=?",
                (job.session_id,),
            )
        service.require_idle_state(path, allow_queued_tail=True)
        with self.assertRaisesRegex(ValueError, "queued job"):
            service.require_idle_state(path)

    def test_retry_wait_is_entered_only_before_execution(self):
        with self.assertRaisesRegex(StateError, "pre-execution"):
            self.state.schedule_provider_job_retry(
                self.job_id, self.token, error_code="example", delay_seconds=1
            )
        self.assertEqual(self.state.get_provider_job(self.job_id).status, "executing")

    def test_receipt_starts_keep_queued_work_blocking_without_a_consumer_scan(self):
        self.queued_tail()
        self.complete_checkpoint()
        self.expire_lease()
        self.first_boot_start(return_value=lambda: self.fail("no provider access"))
        with (
            patch.object(service, "codex_consumer_running") as scan,
            patch.object(service, "_receipt_valid", return_value=True),
            patch.object(service, "boot_identity", return_value=("example-boot", 1.0)),
            patch.object(service, "verify_pins"),
            patch.object(service, "refuse_existing_servers"),
            self.assertRaisesRegex(ValueError, "queued job"),
        ):
            service.authorize_start(
                self.manifest, self.runtime / "receipt", self.runtime, self.hub_config
            )
        scan.assert_not_called()

    def test_failed_reconciliation_falls_through_to_the_unchanged_refusal(self):
        self.complete_checkpoint()
        self.expire_lease()
        with (
            patch.object(reconcile, "reconcile_previous_boot", side_effect=StateError("x")),
            patch.object(service, "codex_consumer_running", return_value=None),
            patch.object(service, "boot_identity", return_value=("example-boot", 1.0)),
            patch.object(service, "require_lingering"),
            patch.object(service, "refuse_native_clients") as clients,
            patch.object(service, "refuse_existing_servers"),
            patch.object(service, "verify_manifest", return_value=self.runtime / "absent.sock"),
            patch.object(service, "remove_previous_boot_link"),
            patch.object(service, "verify_pins"),
            self.assertRaisesRegex(ValueError, "executing job"),
        ):
            service.authorize_start(self.manifest, None, self.runtime, self.hub_config)
        self.assertEqual(clients.call_count, 2)
        self.assertEqual(self.state.get_provider_job(self.job_id).status, "executing")

    def test_a_different_state_database_refuses_before_any_client_starts(self):
        self.expire_lease()
        other = self.runtime / "other.db"
        other.write_bytes(b"")
        self.manifest["state_path"] = str(other)
        with self.assertRaisesRegex(StateError, "different state database"):
            self.reconcile(factory=lambda: self.fail("no client before identity check"))
        self.assertEqual(self.state.get_provider_job(self.job_id).status, "executing")

    def test_receipt_starts_and_starts_without_hub_config_never_reconcile(self):
        self.complete_checkpoint()
        self.expire_lease()
        with (
            patch.object(service, "reconcile_interrupted_executions") as reconciled,
            patch.object(service, "_receipt_valid", return_value=True),
            patch.object(service, "boot_identity", return_value=("example-boot", 1.0)),
            patch.object(service, "verify_pins"),
            patch.object(service, "refuse_existing_servers"),
        ):
            with self.assertRaisesRegex(ValueError, "executing job"):
                service.authorize_start(
                    self.manifest, self.runtime / "receipt", self.runtime, self.hub_config
                )
            reconciled.assert_not_called()
        with (
            patch.object(service, "reconcile_interrupted_executions") as reconciled,
            patch.object(service, "boot_identity", return_value=("other-boot", 1.0)),
            patch.object(service, "require_lingering"),
            patch.object(service, "refuse_native_clients"),
            patch.object(service, "refuse_existing_servers"),
            patch.object(service, "verify_manifest", return_value=self.runtime / "absent.sock"),
            patch.object(service, "remove_previous_boot_link"),
            patch.object(service, "verify_pins"),
            self.assertRaisesRegex(ValueError, "executing job"),
        ):
            fresh = tempfile.TemporaryDirectory()
            self.addCleanup(fresh.cleanup)
            service.authorize_start(self.manifest, None, Path(fresh.name))
        reconciled.assert_not_called()

    def test_refused_first_start_spends_the_boot_and_cannot_reconcile_again(self):
        self.keep_lease()
        with (
            patch.object(reconcile, "DEFAULT_DEADLINE_SECONDS", 0.0),
            self.assertRaisesRegex(ValueError, "executing job"),
        ):
            self.first_boot_start(return_value=lambda: self.fail("no provider access"))
        self.expire_lease()
        with (
            patch.object(service, "reconcile_interrupted_executions") as reconciled,
            self.assertRaisesRegex(ValueError, "fresh start receipt"),
        ):
            self.first_boot_start(return_value=lambda: self.fail("no provider access"))
        reconciled.assert_not_called()

    def test_stdio_factory_bounds_the_handshake_and_reaps_a_failed_child(self):
        closed: list[bool] = []
        deadlines: list[float] = []

        class Transport:
            def close(self) -> None:
                closed.append(True)

        class Client:
            def __init__(self, transport, **_: object) -> None:
                self.transport = transport

            def initialize(self, *, deadline: float) -> None:
                deadlines.append(deadline)
                raise TimeoutError("fictional hung handshake")

        with (
            patch.object(
                reconcile.StdioJsonLineTransport, "start", return_value=Transport()
            ) as start,
            patch.object(reconcile, "CodexAppServerClient", Client),
            patch.object(reconcile.time, "monotonic", return_value=100.0),
        ):
            factory = reconcile.stdio_client_factory(
                Path("/example/pinned/codex"), model_provider=None, permission_profile=None
            )
            with self.assertRaises(TimeoutError):
                factory()
            capped = reconcile.stdio_client_factory(
                Path("/example/pinned/codex"),
                model_provider=None,
                permission_profile=None,
                deadline=105.0,
            )
            with self.assertRaises(TimeoutError):
                capped()
        self.assertEqual(start.call_count, 2)
        start.assert_called_with("/example/pinned/codex")
        self.assertEqual(deadlines, [100.0 + reconcile.INITIALIZE_SECONDS, 105.0])
        self.assertEqual(closed, [True, True])

    def test_manifest_names_the_hub_config_only_as_an_absolute_path(self):
        self.assertIsNone(service.hub_config_path({}))
        self.assertEqual(
            service.hub_config_path({"hub_config": "/home/example/hub.json"}),
            Path("/home/example/hub.json"),
        )
        for value in ("relative/hub.json", 7, ""):
            with self.assertRaisesRegex(ValueError, "invalid Hub config"):
                service.hub_config_path({"hub_config": value})

    def test_a_claimed_read_ends_before_the_deadline_minus_cleanup(self):
        ExecutionJournal(self.state).record_turn(self.job_id, self.token, "example-turn")
        self.expire_lease()
        deadlines: list[float] = []

        class Client:
            def read_turn_outcome(self, *, deadline: float, **_: object):
                deadlines.append(deadline)
                raise TimeoutError("fictional slow read")

            def close(self) -> None:
                pass

        clock = FakeClock()
        clock.now = 1000.0
        self.assertEqual(self.reconcile(clock, factory=Client), 1)
        self.assertEqual(
            deadlines,
            [1000.0 + reconcile.DEFAULT_DEADLINE_SECONDS - reconcile.CLEANUP_SECONDS],
        )
        job = self.state.get_provider_job(self.job_id)
        self.assertEqual((job.status, job.attempt_count), ("indeterminate", 1))

    def test_a_job_too_late_for_a_full_read_stays_in_place(self):
        self.complete_checkpoint()
        self.expire_lease()
        budget = reconcile.CLEANUP_SECONDS + reconcile.CLAIM_RESERVE_SECONDS
        self.assertEqual(self.reconcile(deadline_seconds=budget), 0)
        self.assertEqual(self.state.get_provider_job(self.job_id).status, "executing")

    def test_an_existing_socket_path_refuses_before_any_reconciliation(self):
        self.complete_checkpoint()
        self.expire_lease()
        occupied = self.runtime / "occupied.sock"
        occupied.write_bytes(b"")
        with (
            patch.object(service, "reconcile_interrupted_executions") as reconciled,
            patch.object(service, "boot_identity", return_value=("example-boot", 1.0)),
            patch.object(service, "require_lingering"),
            patch.object(service, "refuse_native_clients"),
            patch.object(service, "refuse_existing_servers"),
            patch.object(service, "verify_manifest", return_value=occupied),
            patch.object(service, "remove_previous_boot_link"),
            self.assertRaisesRegex(ValueError, "existing socket or path"),
        ):
            service.authorize_start(self.manifest, None, self.runtime, self.hub_config)
        reconciled.assert_not_called()
        self.assertEqual(self.state.get_provider_job(self.job_id).status, "executing")


if __name__ == "__main__":
    unittest.main()
