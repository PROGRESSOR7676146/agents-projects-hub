from __future__ import annotations

import sqlite3
import unittest

from hermes_codex_router.root_blockers import persistent_root_blocker
from hermes_codex_router.state import StateError
from tests import test_claude_session_binding as binding_fixtures


class ClaudeModelContinuityTests(unittest.TestCase):
    def setUp(self):
        self.fixture = binding_fixtures.ClaudeSessionBindingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.state = self.fixture.state
        self.topic = self.fixture.topic

    def select(self, *, model="example-next", effort="medium", expected=None):
        return self.state.replace_active_session(
            self.topic.topic_id,
            model=model,
            effort=effort,
            runtime="claude",
            expected_session_id=expected or self.fixture.session.session_id,
        )

    def finish_prior(self):
        job = self.fixture.enqueue(1)
        self.fixture.finish_prior(job, self.fixture.execute(job))
        return job

    def test_model_and_effort_changes_preserve_identity_provenance_and_prior_snapshots(self):
        self.finish_prior()
        previous = self.state.get_session(self.fixture.session.session_id)
        before = self.fixture.snapshot()
        for model, effort in (("example-next", "high"), ("example-next", "medium")):
            selected = self.select(model=model, effort=effort)
            self.assertEqual(
                (selected.session_id, selected.generation, selected.provider_session_id),
                (previous.session_id, previous.generation, previous.provider_session_id),
            )
            self.assertEqual((selected.model, selected.effort), (model, effort))
        after = self.fixture.snapshot()
        for table in ("provider_jobs", "provider_execution_checkpoints"):
            self.assertEqual(after[table], before[table])
        self.fixture.session = selected
        current = self.fixture.enqueue(2, selected.provider_session_id)
        binding = self.fixture.prepare(current, self.fixture.execute(current))
        self.assertEqual(binding.session_id, previous.provider_session_id)
        self.assertFalse(binding.is_new)
        self.assertEqual((current.model, current.effort), ("example-next", "medium"))

    def test_switch_back_preserves_both_provider_sessions_with_different_settings(self):
        self.finish_prior()
        previous = self.state.get_session(self.fixture.session.session_id)
        other = self.state.activate_agent(
            self.topic.topic_id,
            "opencode",
            "example-other",
            "default",
            expected_session_id=previous.session_id,
        )
        other = self.state.bind_provider_session(other.session_id, "example-other-session", None)
        returned = self.state.activate_agent(
            self.topic.topic_id,
            "claude",
            "example-next",
            "medium",
            expected_session_id=other.session_id,
        )
        selected = self.select(expected=returned.session_id)
        self.assertEqual(selected.session_id, previous.session_id)
        self.assertEqual(selected.generation, previous.generation)
        self.assertEqual(selected.provider_session_id, previous.provider_session_id)
        retained = self.state.get_session(other.session_id)
        self.assertEqual(retained.provider_session_id, other.provider_session_id)
        self.assertEqual(retained.status, "satellite")

    def test_runtime_identity_does_not_assume_agent_display_id(self):
        selected = self.state.replace_active_session(
            self.topic.topic_id,
            model="example-next",
            effort="medium",
            runtime="opencode",
            expected_session_id=self.fixture.session.session_id,
        )
        self.assertNotEqual(selected.session_id, self.fixture.session.session_id)
        custom = self.state.activate_agent(
            self.topic.topic_id,
            "example-custom",
            "example-model",
            "high",
            expected_session_id=selected.session_id,
        )
        preserved = self.state.replace_active_session(
            self.topic.topic_id,
            model="example-next",
            effort="medium",
            runtime="claude",
            expected_session_id=custom.session_id,
        )
        self.assertEqual(preserved.session_id, custom.session_id)

    def test_missing_snapshot_stale_snapshot_and_nontelegram_writers_refuse_atomically(self):
        before = self.fixture.snapshot()
        with self.assertRaises(StateError):
            self.state.replace_active_session(
                self.topic.topic_id,
                model="example-next",
                effort="medium",
                runtime="claude",
            )
        self.assertEqual(self.fixture.snapshot(), before)
        with self.assertRaises(StateError):
            self.select(expected="example-stale")
        self.assertEqual(self.fixture.snapshot(), before)
        for writer in ("local", "terminal"):
            with self.state._connection:
                self.state._connection.execute(
                    "UPDATE agent_sessions SET writer_mode=? WHERE session_id=?",
                    (writer, self.fixture.session.session_id),
                )
            before = self.fixture.snapshot()
            with self.assertRaises(StateError):
                self.select()
            self.assertEqual(self.fixture.snapshot(), before)

    def test_queued_and_executing_work_refuse_without_retargeting(self):
        job = self.fixture.enqueue(1)
        for executing in (False, True):
            if executing:
                self.fixture.execute(job)
            before = self.fixture.snapshot()
            with self.assertRaises(StateError):
                self.select()
            self.assertEqual(self.fixture.snapshot(), before)

    def test_transaction_fault_rolls_back_settings_without_replacing_generation(self):
        with self.state._connection:
            self.state._connection.execute(
                "CREATE TRIGGER selection_fault BEFORE UPDATE ON agent_sessions "
                "BEGIN SELECT RAISE(ABORT, 'fictional selection fault'); END"
            )
        before = self.fixture.snapshot()
        with self.assertRaises(sqlite3.DatabaseError):
            self.select()
        self.assertEqual(self.fixture.snapshot(), before)
        self.assertFalse(self.state._connection.in_transaction)

    def test_uncertain_execution_remains_excluded_after_preference_update(self):
        job = self.fixture.enqueue(1)
        token = self.fixture.execute(job)
        self.fixture.journal.record_thread(
            job.job_id, token, binding_fixtures.NATIVE_UUID, self.fixture.root
        )
        self.state.mark_provider_job_indeterminate(job.job_id, token, error_code="example-unknown")
        before = self.fixture.snapshot()
        blocker = persistent_root_blocker(self.state._connection, topic_id=self.topic.topic_id)
        self.assertIsNotNone(blocker)
        selected = self.select()
        self.assertEqual(
            persistent_root_blocker(self.state._connection, topic_id=self.topic.topic_id), blocker
        )
        for table in ("provider_jobs", "provider_execution_checkpoints"):
            self.assertEqual(self.fixture.snapshot()[table], before[table])
        self.fixture.session = selected
        with self.assertRaises(StateError):
            self.fixture.enqueue(2, selected.provider_session_id)
        self.assertIsNone(self.state.lease_provider_job("claude", "example-worker"))

    def test_new_explicitly_creates_fresh_generation_and_native_identity(self):
        self.finish_prior()
        self.select()
        fresh = self.state.new_active_session(
            self.topic.topic_id, expected_session_id=self.fixture.session.session_id
        )
        self.assertNotEqual(fresh.session_id, self.fixture.session.session_id)
        self.assertEqual(fresh.generation, self.fixture.session.generation + 1)
        self.assertIsNone(fresh.provider_session_id)
        self.assertEqual(self.state.get_session(self.fixture.session.session_id).status, "archived")
        self.fixture.session = fresh
        current = self.fixture.enqueue(2)
        binding = self.fixture.prepare(current, self.fixture.execute(current))
        self.assertTrue(binding.is_new)
        self.assertNotEqual(binding.session_id, binding_fixtures.NATIVE_UUID)


if __name__ == "__main__":
    unittest.main()
