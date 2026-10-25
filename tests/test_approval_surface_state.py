from __future__ import annotations

import unittest
from contextlib import closing
from datetime import timedelta

from hermes_codex_router.approval_observations import identity_digest
from hermes_codex_router.approval_surface import assess_surface
from hermes_codex_router.approval_surface_state import pending_approval_waits
from hermes_codex_router.schema_preacceptance_approvals import PREACCEPTANCE_APPROVAL_SCHEMA
from tests import test_task_activity as fixtures


def clone_row(db, table, row, **changes):
    values = dict(row)
    values.update(changes)
    db.execute(
        f"INSERT INTO {table} ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
        tuple(values.values()),
    )


class ApprovalSurfaceStateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TaskActivityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.db.executescript(PREACCEPTANCE_APPROVAL_SCHEMA)
        for table in ("provider_jobs", "agent_sessions", "provider_execution_checkpoints"):
            self.fixture.db.execute(f"ALTER TABLE {table} ADD COLUMN codex_permission_profile TEXT")
        self.fixture.bind()
        self.fixture.event("approval_requested", request=7)

    def waits(self):
        return pending_approval_waits(
            self.fixture.db,
            now=self.fixture.now + timedelta(seconds=60),
            selected_profile=lambda: None,
        )

    def test_exact_persisted_identity_and_notice_delivery_never_prove_card_sent(self):
        f = self.fixture
        f.db.execute("UPDATE task_lifecycle_notices SET status='delivered',telegram_message_id=42")
        f.db.commit()
        before = f.db.total_changes
        waits, truncated = self.waits()
        self.assertFalse(truncated)
        self.assertEqual(len(waits), 1)
        self.assertEqual((waits[0].thread_id, waits[0].turn_id), ("thread", "turn"))
        self.assertEqual(
            assess_surface(waits, None, now=f.now + timedelta(seconds=60))["unknown"], 1
        )
        self.assertEqual(f.db.total_changes, before)

    def test_exact_resolve_and_replacement_writer_invalidate_projection(self):
        self.fixture.event("approval_resolved", request=8)
        self.assertEqual(len(self.waits()[0]), 1)
        self.fixture.event("approval_resolved", request=7)
        self.assertEqual(self.waits()[0], ())
        self.fixture.event("approval_requested", request=9)
        self.fixture.db.execute("UPDATE agent_sessions SET writer_mode='local'")
        self.assertEqual(self.waits()[0], ())

    def test_invalid_or_naive_observation_time_cannot_create_grace(self):
        for value in ("not-a-time", "2026-01-01T00:00:00"):
            with self.subTest(value=value):
                self.fixture.db.execute("UPDATE task_lifecycle_notices SET created_at=?", (value,))
                waits, _ = self.waits()
                counts = assess_surface(waits, None, now=self.fixture.now + timedelta(seconds=60))
                self.assertEqual(counts["unknown"], 1)
                self.assertEqual(counts["grace"], 0)

    def test_history_is_filtered_before_current_limit(self):
        db = self.fixture.db
        activity = db.execute("SELECT * FROM task_activity").fetchone()
        job = db.execute("SELECT * FROM provider_jobs").fetchone()
        checkpoint = db.execute("SELECT * FROM provider_execution_checkpoints").fetchone()
        entry = db.execute("SELECT * FROM task_activity_entries").fetchone()
        for i in range(260):
            job_id = f"aaa-history-{i:03}"
            clone_row(db, "provider_jobs", job, job_id=job_id)
            clone_row(db, "provider_execution_checkpoints", checkpoint, job_id=job_id)
            clone_row(db, "task_activity", activity, job_id=job_id)
            clone_row(db, "task_activity_entries", entry, job_id=job_id)
        for sql in (
            "UPDATE provider_jobs SET status='completed' WHERE job_id LIKE 'aaa-%'",
            "UPDATE provider_jobs SET status='executing',lease_token='replaced' WHERE job_id LIKE 'aaa-%'",
            "UPDATE provider_jobs SET lease_token='lease',lease_expires_at='2025-01-01' WHERE job_id LIKE 'aaa-%'",
            "UPDATE provider_jobs SET lease_expires_at='2026-01-02'; "
            "UPDATE task_activity SET session_generation=2 WHERE job_id LIKE 'aaa-%'",
        ):
            with self.subTest(sql=sql):
                db.executescript(sql)
                waits, truncated = self.waits()
                self.assertEqual(len(waits), 1)
                self.assertFalse(truncated)

    def test_current_requests_over_limit_remain_truncated(self):
        for request in range(257):
            self.fixture.event("approval_requested", request=request)
        waits, truncated = self.waits()
        self.assertEqual(len(waits), 256)
        self.assertTrue(truncated)


class PreparedApprovalSurfaceTests(unittest.TestCase):
    def setUp(self):
        from tests import test_preacceptance_approvals as early

        self.f = early.PreacceptanceApprovalTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_missing_profile_context_retains_unknown_projection(self):
        from hermes_codex_router.state import HubState

        f = self.f
        f.observe(f.scope())
        with closing(HubState.open_existing(f.root / "state.db")) as state:
            waits, incomplete = state.pending_codex_approvals(now=f.now)
        self.assertEqual(waits, ())
        self.assertTrue(incomplete)

    def test_exact_early_identity_resolves_and_old_runtime_never_reopens(self):
        f = self.f
        scope = f.scope()
        f.observe(scope)
        waits, truncated = f.state.pending_codex_approvals(now=f.now)
        self.assertFalse(truncated)
        self.assertEqual(len(waits), 1)
        self.assertEqual((waits[0].thread_id, waits[0].turn_id), ("example-thread", "example-turn"))
        f.observe(scope, kind="approval_resolved", request=8)
        self.assertEqual(len(f.state.pending_codex_approvals(now=f.now)[0]), 1)
        f.observe(scope, kind="approval_resolved", request=7)
        self.assertEqual(f.state.pending_codex_approvals(now=f.now)[0], ())
        f.observe(scope, request=9)
        f.early.register_runtime(
            agent_id="codex",
            worker_slot=1,
            instance_token="example-instance-b",
            now=f.now + timedelta(seconds=1),
        )
        self.assertEqual(f.state.pending_codex_approvals(now=f.now + timedelta(seconds=1))[0], ())

    def test_promotion_projects_one_current_request_without_replacing_identity(self):
        f = self.f
        scope = f.scope()
        f.observe(scope)
        before = f.state.pending_codex_approvals(now=f.now)[0]
        f.journal.record_turn(f.job.job_id, f.token, "example-turn")
        f.promote(scope)
        self.assertEqual(f.state.pending_codex_approvals(now=f.now)[0], before)

    def test_conflicting_accepted_turn_excludes_open_scope(self):
        f = self.f
        scope = f.scope()
        f.observe(scope)
        f.journal.record_turn(f.job.job_id, f.token, "different-turn")
        self.assertEqual(f.state.pending_codex_approvals(now=f.now), ((), False))

    def test_early_writer_lease_and_profile_fences_survive_sql_filtering(self):
        f = self.f
        scope = f.scope()
        f.observe(scope)
        db = f.state._connection
        for sql in (
            "UPDATE agent_sessions SET writer_mode='local'",
            "UPDATE provider_jobs SET lease_token='replaced'",
            "UPDATE provider_jobs SET lease_expires_at='2000-01-01'",
            "UPDATE preacceptance_scopes SET codex_permission_profile='other'",
        ):
            with self.subTest(sql=sql):
                db.execute("SAVEPOINT fixture")
                db.execute(sql)
                self.assertEqual(
                    pending_approval_waits(db, now=f.now, selected_profile=lambda: None),
                    ((), False),
                )
                db.execute("ROLLBACK TO fixture")
                db.execute("RELEASE fixture")

    def test_current_early_result_limit_is_still_bounded(self):
        f = self.f
        scope = f.scope()
        f.observe(scope)
        db = f.state._connection
        entry = db.execute("SELECT * FROM preacceptance_requests").fetchone()
        for request in range(260):
            clone_row(
                db,
                "preacceptance_requests",
                entry,
                identity=identity_digest(f"current-{request}"),
                event_key=f"example-current-{request}",
            )
        db.commit()
        waits, truncated = f.state.pending_codex_approvals(now=f.now)
        self.assertEqual(len(waits), 256)
        self.assertTrue(truncated)

    def test_obsolete_runtime_history_does_not_starve_current_wait(self):
        f = self.f
        scope = f.scope()
        f.observe(scope)
        db = f.state._connection
        old = db.execute("SELECT * FROM preacceptance_requests").fetchone()
        for request in range(260):
            clone_row(
                db,
                "preacceptance_requests",
                old,
                identity=identity_digest(f"history-{request}"),
                event_key=f"example-history-{request}",
            )
        db.execute("UPDATE preacceptance_runtime_epochs SET epoch=epoch+1")
        db.commit()
        job, token, root = f.next_prepared_job()
        runtime = f.early.register_runtime(
            agent_id="codex", worker_slot=2, instance_token="example-current", now=f.now
        )
        current = f.early.open_scope(
            job.job_id,
            token,
            runtime,
            provider_thread_id="example-next-thread",
            project_root=str(root),
            now=f.now,
        )
        from hermes_codex_router.codex_activity import CodexActivityEvent

        f.early.observe(
            current,
            runtime,
            CodexActivityEvent(
                "approval_requested", "command", "example-next-thread", "next-turn", "item", 7
            ),
            now=f.now,
        )
        waits, truncated = f.state.pending_codex_approvals(now=f.now)
        self.assertFalse(truncated)
        self.assertEqual(len(waits), 1)
        self.assertEqual(waits[0].turn_id, "next-turn")
