"""Ingress maintenance consumes the shared claim before any native access."""

from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from hermes_codex_router import codex_late_control
from hermes_codex_router.codex_appserver import StoredTurnOutcome, TurnResult
from tests import test_codex_ingress_live as fixtures
from tests.test_telegram_turn_provenance import row_values


class CodexIngressMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexIngressLiveTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.job = self.fixture.job
        self.config = self.fixture.fixture.harness.config
        self.state.terminate_provider_job_with_notice(
            self.job.job_id,
            self.fixture.token,
            status="indeterminate",
            error_class="ambiguous_execution",
            error_code="example-ingress-loss",
            sender_agent_id="codex",
            telegram_html="Example retained uncertain notice",
        )

    def sweep(self):
        return codex_late_control.CodexIngressSweep()

    def once(self, sweep, client):
        return sweep.run_once(
            self.state,
            self.config,
            worker_id="example-ingress-maintainer",
            agent_id="codex",
            client_factory=lambda deadline: client,
        )

    def row(self):
        return row_values(self.state.codex_controls.read(self.job.job_id))

    def test_due_ingress_claim_precedes_connection_and_sends_once(self):
        sweep = self.sweep()
        client = fixtures.Client()
        original = client.read_turn_outcome

        def read(**kwargs):
            self.assertEqual(self.row()["late_read_attempts"], 1)
            self.assertIsNotNone(self.row()["read_claim_token"])
            return original(**kwargs)

        client.read_turn_outcome = read
        self.assertTrue(self.once(sweep, client))
        self.assertEqual(client.calls, ["read", "interrupt", "read", "close"])
        self.assertIsNone(self.row()["read_claim_token"])
        self.assertIsNotNone(self.state.codex_ingress_control.read_cause(self.job.job_id))
        self.assertIsNone(self.state.pending_emergency_stop_for_job(self.job.job_id))
        self.assertFalse(self.once(sweep, fixtures.Client()))
        self.assertEqual(self.row()["late_read_attempts"], 1)

    def test_stop_arriving_after_claim_uses_same_proof_and_one_cycle(self):
        client = fixtures.Client()
        original = client.read_turn_outcome

        def request_stop(**kwargs):
            if len(client.calls) == 0:
                self.state.request_emergency_stop(
                    topic_id=self.job.topic_id,
                    chat_id=self.job.chat_id,
                    message_id=99,
                    target_agent_id="codex",
                )
            return original(**kwargs)

        client.read_turn_outcome = request_stop
        self.assertTrue(self.once(self.sweep(), client))
        self.assertEqual(self.row()["late_read_attempts"], 1)
        self.assertEqual(client.calls.count("interrupt"), 1)
        self.assertEqual(self.row()["interrupt_source"], "late")
        self.assertIsNotNone(self.row()["stop_request_id"])
        self.assertIsNone(self.state.codex_ingress_control.read_cause(self.job.job_id))

    def test_recovery_after_claim_refuses_send_without_refunding_allowance(self):
        client = fixtures.Client()
        original = client.read_turn_outcome

        def recover(**kwargs):
            self.state.telegram_ingress.record_poll(
                self.fixture.publisher,
                sequence=4,
                succeeded=True,
                observed_at=datetime.now(timezone.utc),
            )
            return original(**kwargs)

        client.read_turn_outcome = recover
        self.assertTrue(self.once(self.sweep(), client))
        self.assertEqual(client.calls, ["read", "close"])
        self.assertEqual(self.row()["late_read_attempts"], 1)
        self.assertIsNone(self.row()["send_started_at"])

    def test_exact_completion_without_real_stop_recovers_saved_result(self):
        client = fixtures.Client(
            StoredTurnOutcome("completed", TurnResult("Example recovered final", None, None))
        )
        self.assertTrue(self.once(self.sweep(), client))
        self.assertEqual(client.calls, ["read", "close"])
        self.assertEqual(self.state.get_provider_job(self.job.job_id).status, "result_ready")
        self.assertEqual(
            self.state.get_telegram_outbox_for_job(self.job.job_id).telegram_html,
            "Recovered completed Codex result:\n\nExample recovered final",
        )

    def test_post_fence_completion_preserves_unknown_delivery_and_unquiesced_owner(self):
        client = fixtures.Client()

        def unknown(**kwargs):
            client.calls.append("interrupt")
            raise OSError("Example unknown native send")

        client.interrupt_turn = unknown
        self.assertTrue(self.once(self.sweep(), client))
        cause = row_values(self.state.codex_ingress_control.read_cause(self.job.job_id))
        owner = self.row()["send_owner_token_hash"]
        self.assertIsNone(self.row()["owner_quiesced_at"])
        lease = self.state.lease_telegram_outbox("codex", "example-sender")
        assert lease is not None and lease.lease_token is not None
        self.state.delivery.begin_outbox_send(lease.outbox_id, lease.lease_token, 1)
        self.state.delivery.mark_outbox_unknown(
            lease.outbox_id,
            lease.lease_token,
            1,
            error_code="example-lost-delivery",
        )
        outbox = self.state.get_telegram_outbox_for_job(self.job.job_id)
        parts = self.state.get_telegram_outbox_parts(outbox.outbox_id)
        after = datetime.now(timezone.utc) + timedelta(seconds=31)
        completed = fixtures.Client(
            StoredTurnOutcome("completed", TurnResult("Example post-fence raw final", None, None))
        )
        with patch("hermes_codex_router.codex_ingress_control._now", return_value=after):
            self.assertTrue(self.once(self.sweep(), completed))
        self.assertEqual(completed.calls, ["read", "close"])
        self.assertEqual(self.row()["late_read_attempts"], 2)
        self.assertEqual(self.row()["send_owner_token_hash"], owner)
        self.assertIsNone(self.row()["owner_quiesced_at"])
        self.assertEqual(
            row_values(self.state.codex_ingress_control.read_cause(self.job.job_id)), cause
        )
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job.job_id), outbox)
        self.assertEqual(self.state.get_telegram_outbox_parts(outbox.outbox_id), parts)
        self.assertEqual(self.state.get_provider_job(self.job.job_id).status, "indeterminate")
        saved = self.state._connection.execute(
            "SELECT completed_text FROM provider_execution_checkpoints WHERE job_id=?",
            (self.job.job_id,),
        ).fetchone()[0]
        self.assertEqual(saved, "Example post-fence raw final")
        with patch(
            "hermes_codex_router.codex_ingress_control._now",
            return_value=after + timedelta(seconds=31),
        ):
            self.assertFalse(self.once(self.sweep(), fixtures.Client()))

    def test_candidate_page_is_read_only_and_agent_filtered(self):
        statements = []
        self.state._connection.set_trace_callback(statements.append)
        try:
            ids, upper = self.state.codex_ingress_control.candidate_page("codex")
            other, _ = self.state.codex_ingress_control.candidate_page("example-other-agent")
        finally:
            self.state._connection.set_trace_callback(None)
        self.assertEqual(ids, (self.job.job_id,))
        self.assertEqual(upper, self.job.job_id)
        self.assertEqual(other, ())
        self.assertFalse(any(sql.upper().startswith("BEGIN") for sql in statements))

    def test_keyset_scan_advances_beyond_failed_pages_and_freezes_sweep_end(self):
        sweep = self.sweep()
        pages = []
        claims = []

        def page(agent_id, *, after=None, through=None, limit=32, now=None):
            pages.append((after, through, limit))
            start = 0 if after is None else int(after) + 1
            end = min(start + limit, 70)
            return tuple(f"{n:03}" for n in range(start, end)), "069"

        def claim(job_id, worker_id, **kwargs):
            claims.append(job_id)
            if job_id == "001":
                raise ValueError("Example malformed candidate")
            return None

        with (
            patch.object(self.state.codex_ingress_control, "candidate_page", side_effect=page),
            patch.object(self.state.codex_ingress_control, "claim_read", side_effect=claim),
        ):
            for _ in range(4):
                self.assertFalse(self.once(sweep, fixtures.Client()))
        self.assertEqual(claims, [f"{n:03}" for n in range(70)])
        self.assertEqual(pages, [(None, None, 32), ("031", "069", 32), ("063", "069", 32)])

    def test_maintenance_services_stop_then_ingress_and_rechecks_shutdown(self):
        stop = threading.Event()
        maintenance = codex_late_control.CodexControlMaintenance(
            self.config,
            worker_id="example-maintainer",
            agent_id="codex",
            client_factory=lambda deadline: fixtures.Client(),
            stop=stop,
        )
        calls = []

        def owner_stop(*args, **kwargs):
            calls.append("stop")
            return True

        def ingress(*args, **kwargs):
            calls.append("ingress")
            return True

        with (
            patch.object(codex_late_control, "run_late_control_once", side_effect=owner_stop),
            patch.object(maintenance.ingress, "run_once", side_effect=ingress),
        ):
            self.assertTrue(maintenance.run_once(self.state))
            self.assertEqual(calls, ["stop", "ingress"])
            calls.clear()

            def stop_during_cycle(*args, **kwargs):
                stop.set()
                return owner_stop(*args, **kwargs)

            with patch.object(
                codex_late_control, "run_late_control_once", side_effect=stop_during_cycle
            ):
                self.assertTrue(maintenance.run_once(self.state))
            self.assertEqual(calls, ["stop"])

    def test_shutdown_between_candidates_does_not_consume_next_claim(self):
        stop = threading.Event()
        sweep = self.sweep()
        original = self.state.codex_ingress_control.claim_read
        first = "000-example-skipped"
        seen = []

        def claim(job_id, *args, **kwargs):
            seen.append(job_id)
            if job_id == first:
                stop.set()
                return None
            return original(job_id, *args, **kwargs)

        with (
            patch.object(
                self.state.codex_ingress_control,
                "candidate_page",
                return_value=((first, self.job.job_id), self.job.job_id),
            ),
            patch.object(self.state.codex_ingress_control, "claim_read", side_effect=claim),
        ):
            self.assertFalse(
                sweep.run_once(
                    self.state,
                    self.config,
                    worker_id="example-ingress-maintainer",
                    agent_id="codex",
                    client_factory=lambda deadline: self.fail("Shutdown must prevent connection"),
                    stopped=stop.is_set,
                )
            )
        self.assertEqual(seen, [first])
        self.assertEqual(sweep.after, first)
        self.assertEqual(self.row()["late_read_attempts"], 0)

    def _candidate(self, index, ingress):
        """Fictional retained rows through the real immutable SQL fences."""
        db = self.state._connection
        job_id = f"zz-example-page-{index:03}"

        def insert(table, values):
            columns = ",".join(values)
            placeholders = ",".join("?" for _ in values)
            db.execute(
                f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(values.values())
            )

        job = dict(
            db.execute("SELECT * FROM provider_jobs WHERE job_id=?", (self.job.job_id,)).fetchone()
        )
        checkpoint = dict(
            db.execute(
                "SELECT * FROM provider_execution_checkpoints WHERE job_id=?", (self.job.job_id,)
            ).fetchone()
        )
        control = dict(self.row())
        with self.state._immediate_transaction():
            job.update(
                job_id=job_id,
                idempotency_key=job_id,
                message_id=1000 + index,
                topic_sequence=1000 + index,
                status="queued",
                attempt_count=0,
                provider_started_at=None,
            )
            insert("provider_jobs", job)
            self.state.telegram_turn_provenance.record_new_job_in_transaction(job_id, ingress)
            checkpoint.update(job_id=job_id, provider_turn_id=None)
            insert("provider_execution_checkpoints", checkpoint)
            control.update(job_id=job_id, provider_turn_id=f"example-page-turn-{index:03}")
            insert("codex_turn_controls", control)
            insert(
                "codex_telegram_precaution_targets", dict(job_id=job_id, ingress_identity=ingress)
            )
            db.execute(
                "UPDATE provider_execution_checkpoints SET provider_turn_id=? WHERE job_id=?",
                (control["provider_turn_id"], job_id),
            )
            db.execute(
                "UPDATE provider_jobs SET status='indeterminate',attempt_count=1,provider_started_at=? WHERE job_id=?",
                (self.job.provider_started_at, job_id),
            )
        return job_id

    def test_real_keyset_rows_pass_healthy_errors_and_bound_concurrent_insertions(self):
        publisher = self.state.telegram_ingress.register(
            "codex",
            instance_token="example-healthy-controller",
            previous_epoch=0,
            now=datetime.now(timezone.utc),
        )
        self.state.telegram_ingress.record_poll(
            publisher, sequence=1, succeeded=True, observed_at=datetime.now(timezone.utc)
        )
        ids = [self._candidate(index, "hub" if index == 69 else "codex") for index in range(70)]
        # The original target is no longer eligible, while all new rows remain retained.
        with self.state._immediate_transaction():
            self.state._connection.execute(
                "UPDATE provider_jobs SET status='failed' WHERE job_id=?", (self.job.job_id,)
            )
        sweep = self.sweep()
        original = self.state.codex_ingress_control.claim_read
        seen = []

        def claim(job_id, *args, **kwargs):
            seen.append(job_id)
            if job_id == ids[1]:
                raise ValueError("Example corrupt candidate")
            return original(job_id, *args, **kwargs)

        with patch.object(self.state.codex_ingress_control, "claim_read", side_effect=claim):
            self.assertFalse(self.once(sweep, fixtures.Client()))
            self.assertEqual(sweep.through, ids[-1])
            later = self._candidate(999, "hub")
            with self.state._immediate_transaction():
                self.state._connection.execute(
                    "UPDATE provider_jobs SET status='failed' WHERE job_id=?", (ids[64],)
                )
            self.assertFalse(self.once(sweep, fixtures.Client()))
            client = fixtures.Client()
            self.assertTrue(self.once(sweep, client))
        self.assertEqual(seen, [job_id for job_id in ids if job_id != ids[64]])
        self.assertNotIn(later, seen)
        self.assertEqual(client.calls.count("interrupt"), 1)
        self.assertEqual(
            row_values(self.state.codex_controls.read(ids[-1]))["late_read_attempts"], 1
        )
        self.assertEqual(
            row_values(self.state.codex_controls.read(ids[0]))["late_read_attempts"], 0
        )
