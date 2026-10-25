from __future__ import annotations

import unittest
from datetime import timedelta

from hermes_codex_router.approval_surface import assess_surface
from hermes_codex_router.approval_surface_state import pending_approval_waits
from hermes_codex_router.schema_preacceptance_approvals import PREACCEPTANCE_APPROVAL_SCHEMA
from tests import test_task_activity as fixtures


class ApprovalSurfaceStateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TaskActivityTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.fixture.db.executescript(PREACCEPTANCE_APPROVAL_SCHEMA)
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


class PreparedApprovalSurfaceTests(unittest.TestCase):
    def setUp(self):
        from tests import test_preacceptance_approvals as early

        self.f = early.PreacceptanceApprovalTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

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
