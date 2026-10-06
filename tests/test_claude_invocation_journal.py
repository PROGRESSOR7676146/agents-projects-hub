from __future__ import annotations

import inspect
import sqlite3
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hermes_codex_router.codex_failure import MAX_PARTIAL_TEXT
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.state import HubState, StateError

SESSION_UUID = "019abcde-1234-7fff-8fff-0123456789ab"
OTHER_UUID = "00000000-0000-4000-8000-000000000001"
MESSAGE_UUID = str(uuid.UUID(int=uuid.UUID(SESSION_UUID).int + 1))


class ClaudeInvocationJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name)
        self.root = self.base / "example-project"
        self.root.mkdir()
        self.other_root = self.base / "other-example"
        self.other_root.mkdir()
        self.path = self.base / "state.db"
        self.state = HubState.open(self.path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=7,
            title="Fictional Claude topic",
            execution_root=self.root,
        )
        self.session = self.state.activate_agent(
            self.topic.topic_id, "claude", "example-model", "high"
        )
        self.job, created = self.state.enqueue_provider_job(
            idempotency_key="example:1",
            chat_id=self.topic.chat_id,
            message_id=1,
            topic_id=self.topic.topic_id,
            agent_id=self.session.agent_id,
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model=self.session.model,
            effort=self.session.effort,
            payload_text="Fictional Claude request",
        )
        self.assertTrue(created)
        leased = self.state.lease_provider_job("claude", "example-worker")
        assert leased is not None and leased.lease_token is not None
        self.token = leased.lease_token
        self.state.mark_provider_job_executing(self.job.job_id, self.token)
        self.journal = ExecutionJournal(self.state, progress_enabled=True)
        with patch(
            "hermes_codex_router.execution_journal.uuid.uuid4", return_value=uuid.UUID(SESSION_UUID)
        ):
            self.binding = self.journal.prepare_claude_session(
                self.job.job_id, self.token, self.root
            )

    def item(
        self,
        text: Any = "Fictional visible assistant text",
        message_id: Any = MESSAGE_UUID,
        *,
        token: str | None = None,
        session_id: Any = SESSION_UUID,
        cwd: Path | None = None,
    ) -> None:
        self.journal.record_claude_item(
            self.job.job_id,
            self.token if token is None else token,
            session_id,
            message_id,
            text,
            cwd=self.root if cwd is None else cwd,
        )

    def complete(
        self,
        text: Any = "Fictional completed assistant result",
        *,
        token: str | None = None,
        session_id: Any = SESSION_UUID,
        cwd: Path | None = None,
    ) -> None:
        self.journal.record_claude_completion(
            self.job.job_id,
            self.token if token is None else token,
            session_id,
            text,
            cwd=self.root if cwd is None else cwd,
        )

    def snapshot(self) -> tuple[str, ...]:
        return tuple(self.state._connection.iterdump())

    def refuse_both(self, **kwargs: Any) -> None:
        before = self.snapshot()
        with self.assertRaises((StateError, OSError)):
            self.item(**kwargs)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises((StateError, OSError)):
            self.complete(**kwargs)
        self.assertEqual(self.snapshot(), before)

    def mutate(self, sql: str, values: tuple[Any, ...]) -> None:
        with self.state._connection:
            self.state._connection.execute(sql, values)

    def visible_rows(self) -> list[tuple[Any, ...]]:
        return [
            tuple(row)
            for row in self.state._connection.execute(
                "SELECT item_id,phase,visible_text FROM provider_visible_items "
                "WHERE job_id=? ORDER BY sequence",
                (self.job.job_id,),
            )
        ]

    def test_both_methods_require_keyword_only_cwd(self) -> None:
        for method in (
            self.journal.record_claude_item,
            self.journal.record_claude_completion,
        ):
            with self.subTest(method=method.__name__):
                parameter = inspect.signature(method).parameters["cwd"]
                self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
                self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_visible_item_commits_unknown_phase_without_native_turn_or_progress(self) -> None:
        text = "First visible paragraph\n\nSecond paragraph."
        self.item(text)
        self.assertEqual(self.visible_rows(), [(MESSAGE_UUID, "unknown", text)])
        checkpoint = self.journal.read(self.job.job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["provider_thread_id"], self.binding.session_id)
        self.assertIsNone(checkpoint["provider_turn_id"])
        self.assertIsNone(checkpoint["completed_text"])
        self.assertEqual(
            self.state._connection.execute(
                "SELECT count(*) FROM provider_progress_deliveries"
            ).fetchone()[0],
            0,
        )
        with sqlite3.connect(self.path) as observer:
            self.assertEqual(
                observer.execute(
                    "SELECT item_id,phase,visible_text FROM provider_visible_items WHERE job_id=?",
                    (self.job.job_id,),
                ).fetchone(),
                (MESSAGE_UUID, "unknown", text),
            )

    def test_same_message_and_text_are_idempotent_but_changed_text_is_refused(self) -> None:
        self.item("Original visible text")
        before = self.snapshot()
        self.item("Original visible text")
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(StateError):
            self.item("Changed visible text")
        self.assertEqual(self.snapshot(), before)

    def test_existing_item_with_incompatible_phase_is_refused(self) -> None:
        self.item("Original visible text")
        self.mutate(
            "UPDATE provider_visible_items SET phase='commentary' WHERE job_id=? AND item_id=?",
            (self.job.job_id, MESSAGE_UUID),
        )
        before = self.snapshot()
        with self.assertRaises(StateError):
            self.item("Original visible text")
        self.assertEqual(self.snapshot(), before)

    def test_validated_partial_reads_only_current_exact_binding(self) -> None:
        self.item("Provisional text")
        before = self.snapshot()
        self.assertEqual(
            self.journal.validated_claude_partial(
                self.job.job_id, self.token, SESSION_UUID, cwd=self.root
            ),
            "Provisional text",
        )
        self.assertEqual(self.snapshot(), before)
        for token, identifier, root in (
            ("wrong", SESSION_UUID, self.root),
            (self.token, OTHER_UUID, self.root),
            (self.token, SESSION_UUID, self.other_root),
        ):
            with self.subTest(token=token), self.assertRaises(StateError):
                self.journal.validated_claude_partial(self.job.job_id, token, identifier, cwd=root)
            self.assertEqual(self.snapshot(), before)
        self.mutate(
            "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
            (self.session.session_id,),
        )
        with self.assertRaises(StateError):
            self.journal.validated_claude_partial(
                self.job.job_id, self.token, SESSION_UUID, cwd=self.root
            )

    def test_partial_text_preserves_arrival_order(self) -> None:
        self.item("First", OTHER_UUID)
        self.item("Second", MESSAGE_UUID)
        self.assertEqual(self.journal.partial_text(self.job.job_id), "First\n\nSecond")

    def test_partial_text_uses_existing_bounded_suffix(self) -> None:
        text = "A" * MAX_PARTIAL_TEXT + "Visible suffix"
        self.item(text)
        self.assertEqual(
            self.journal.partial_text(self.job.job_id),
            "[Earlier partial text omitted]\n" + text[-(MAX_PARTIAL_TEXT - 40) :],
        )

    def test_completion_persists_exact_text_without_turn_and_supersedes_partial(self) -> None:
        self.item("Earlier provisional text")
        text = "  Exact final text\n\nwith trailing whitespace. \n"
        self.complete(text)
        checkpoint = self.journal.read(self.job.job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["completed_text"], text)
        self.assertIsNone(checkpoint["provider_turn_id"])
        self.assertEqual(self.journal.partial_text(self.job.job_id), text)
        with sqlite3.connect(self.path) as observer:
            self.assertEqual(
                observer.execute(
                    "SELECT completed_text FROM provider_execution_checkpoints WHERE job_id=?",
                    (self.job.job_id,),
                ).fetchone()[0],
                text,
            )

    def test_completion_without_partial_and_empty_completion_are_valid(self) -> None:
        self.complete("")
        checkpoint = self.journal.read(self.job.job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["completed_text"], "")
        self.assertEqual(self.journal.partial_text(self.job.job_id), "")

    def test_same_completion_is_idempotent_but_changed_completion_is_refused(self) -> None:
        self.complete("Completed text")
        before = self.snapshot()
        self.complete("Completed text")
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(StateError):
            self.complete("Different completed text")
        self.assertEqual(self.snapshot(), before)

    def test_all_later_items_are_refused_after_completion_even_exact_duplicates(self) -> None:
        self.item("Earlier text")
        self.complete("Completed text")
        before = self.snapshot()
        for text, message_id in (
            ("Earlier text", MESSAGE_UUID),
            ("Changed earlier text", MESSAGE_UUID),
            ("Late text", OTHER_UUID),
        ):
            with self.subTest(text=text), self.assertRaises(StateError):
                self.item(text, message_id)
            self.assertEqual(self.snapshot(), before)

    def test_exact_live_lease_token_is_required(self) -> None:
        self.refuse_both(token="fictional-wrong-token")
        self.refuse_both(token="")

    def test_expired_execution_lease_refuses_all_writes(self) -> None:
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.mutate(
            "UPDATE provider_jobs SET lease_expires_at=? WHERE job_id=?",
            (expired, self.job.job_id),
        )
        self.refuse_both()

    def test_duplicate_item_still_requires_live_lease(self) -> None:
        self.item("Original visible text")
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.mutate(
            "UPDATE provider_jobs SET lease_expires_at=? WHERE job_id=?",
            (expired, self.job.job_id),
        )
        before = self.snapshot()
        with self.assertRaises(StateError):
            self.item("Original visible text")
        self.assertEqual(self.snapshot(), before)

    def test_duplicate_completion_still_requires_live_lease(self) -> None:
        self.complete("Completed text")
        expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.mutate(
            "UPDATE provider_jobs SET lease_expires_at=? WHERE job_id=?",
            (expired, self.job.job_id),
        )
        before = self.snapshot()
        with self.assertRaises(StateError):
            self.complete("Completed text")
        self.assertEqual(self.snapshot(), before)

    def test_preinvocation_lease_is_insufficient(self) -> None:
        self.mutate("UPDATE provider_jobs SET status='leased' WHERE job_id=?", (self.job.job_id,))
        self.refuse_both()

    def test_terminal_job_refuses_writes_even_with_old_token(self) -> None:
        self.mutate(
            "UPDATE provider_jobs SET status='completed',lease_owner=NULL,"
            "lease_token=NULL,lease_expires_at=NULL WHERE job_id=?",
            (self.job.job_id,),
        )
        self.refuse_both()

    def test_missing_prepared_checkpoint_is_refused(self) -> None:
        self.mutate("DELETE FROM provider_execution_checkpoints WHERE job_id=?", (self.job.job_id,))
        self.refuse_both()

    def test_native_session_mismatch_is_refused(self) -> None:
        self.refuse_both(session_id=OTHER_UUID)

    def test_noncanonical_native_session_ids_are_refused(self) -> None:
        for value in (SESSION_UUID.upper(), SESSION_UUID.replace("-", ""), "bad-uuid", "", None):
            with self.subTest(value=value):
                self.refuse_both(session_id=value)

    def test_noncanonical_identity_is_refused_even_when_all_stored_bindings_match(self) -> None:
        identifier = SESSION_UUID.upper()
        self.mutate(
            "UPDATE agent_sessions SET provider_session_id=? WHERE session_id=?",
            (identifier, self.session.session_id),
        )
        self.mutate(
            "UPDATE provider_execution_checkpoints SET provider_thread_id=? WHERE job_id=?",
            (identifier, self.job.job_id),
        )
        self.mutate(
            "UPDATE provider_jobs SET provider_session_id=? WHERE job_id=?",
            (identifier, self.job.job_id),
        )
        self.refuse_both(session_id=identifier)

    def test_noncanonical_message_ids_are_refused_without_mutation(self) -> None:
        before = self.snapshot()
        for value in (MESSAGE_UUID.upper(), MESSAGE_UUID.replace("-", ""), "bad-uuid", "", None):
            with self.subTest(value=value), self.assertRaises(StateError):
                self.item(message_id=value)
            self.assertEqual(self.snapshot(), before)

    def test_invalid_or_changed_checkpoint_session_is_refused(self) -> None:
        for value in ("bad-uuid", OTHER_UUID, SESSION_UUID.upper()):
            with self.subTest(value=value):
                self.mutate(
                    "UPDATE provider_execution_checkpoints SET provider_thread_id=? WHERE job_id=?",
                    (value, self.job.job_id),
                )
                self.refuse_both()

    def test_incompatible_native_turn_evidence_is_refused(self) -> None:
        self.mutate(
            "UPDATE provider_execution_checkpoints SET provider_turn_id=? WHERE job_id=?",
            (MESSAGE_UUID, self.job.job_id),
        )
        self.refuse_both()

    def test_changed_current_session_identity_is_refused(self) -> None:
        self.mutate(
            "UPDATE agent_sessions SET provider_session_id=? WHERE session_id=?",
            (OTHER_UUID, self.session.session_id),
        )
        self.refuse_both()

    def test_changed_queued_session_snapshot_is_refused(self) -> None:
        self.mutate(
            "UPDATE provider_jobs SET provider_session_id=? WHERE job_id=?",
            (OTHER_UUID, self.job.job_id),
        )
        self.refuse_both()

    def test_changed_generation_is_refused(self) -> None:
        self.mutate(
            "UPDATE agent_sessions SET generation=generation+1 WHERE session_id=?",
            (self.session.session_id,),
        )
        self.refuse_both()

    def test_changed_agent_is_refused(self) -> None:
        self.mutate(
            "UPDATE agent_sessions SET agent_id='codex' WHERE session_id=?",
            (self.session.session_id,),
        )
        self.refuse_both()

    def test_changed_topic_is_refused(self) -> None:
        topic = self.state.observe_topic(
            project_id="other-example",
            chat_id=self.topic.chat_id,
            thread_id=8,
            title="Another fictional topic",
            execution_root=self.other_root,
        )
        self.mutate(
            "UPDATE agent_sessions SET topic_id=? WHERE session_id=?",
            (topic.topic_id, self.session.session_id),
        )
        self.refuse_both()

    def test_changed_job_chat_is_refused(self) -> None:
        self.mutate(
            "UPDATE provider_jobs SET chat_id=? WHERE job_id=?",
            (-1002222222222, self.job.job_id),
        )
        self.refuse_both()

    def test_local_or_terminal_writer_is_refused(self) -> None:
        for writer in ("local", "terminal"):
            with self.subTest(writer=writer):
                self.mutate(
                    "UPDATE agent_sessions SET writer_mode=? WHERE session_id=?",
                    (writer, self.session.session_id),
                )
                self.refuse_both()

    def test_archived_session_is_refused(self) -> None:
        self.mutate(
            "UPDATE agent_sessions SET status='archived' WHERE session_id=?",
            (self.session.session_id,),
        )
        self.refuse_both()

    def test_nonarchived_satellite_telegram_session_is_supported(self) -> None:
        self.mutate(
            "UPDATE agent_sessions SET status='satellite' WHERE session_id=?",
            (self.session.session_id,),
        )
        self.item("Satellite visible text")
        self.complete("Satellite completed text")
        self.assertEqual(self.journal.partial_text(self.job.job_id), "Satellite completed text")

    def test_canonical_root_alias_is_supported(self) -> None:
        alias = self.base / "root-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        self.item("Visible text", cwd=alias)
        self.complete("Completed text", cwd=alias)
        checkpoint = self.journal.read(self.job.job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["project_root"], str(self.root.resolve()))

    def test_wrong_or_missing_caller_root_is_refused(self) -> None:
        self.refuse_both(cwd=self.other_root)
        self.refuse_both(cwd=self.base / "nonexistent-example")
        file = self.base / "fictional-file.txt"
        file.write_text("Fictional text", encoding="utf-8")
        self.refuse_both(cwd=file)

    def test_changed_checkpoint_root_is_refused(self) -> None:
        self.mutate(
            "UPDATE provider_execution_checkpoints SET project_root=? WHERE job_id=?",
            (str(self.other_root.resolve()), self.job.job_id),
        )
        self.refuse_both()

    def test_changed_topic_scope_is_refused(self) -> None:
        self.mutate(
            "UPDATE topics SET execution_scope=? WHERE topic_id=?",
            (f"root:{self.other_root.resolve()}", self.topic.topic_id),
        )
        self.refuse_both()

    def test_checkpoint_and_topic_root_drift_cannot_replace_authorized_caller_root(self) -> None:
        self.mutate(
            "UPDATE provider_execution_checkpoints SET project_root=? WHERE job_id=?",
            (str(self.other_root.resolve()), self.job.job_id),
        )
        self.mutate(
            "UPDATE topics SET execution_scope=? WHERE topic_id=?",
            (f"root:{self.other_root.resolve()}", self.topic.topic_id),
        )
        self.refuse_both(cwd=self.root)

    def test_legacy_scope_does_not_bypass_checkpoint_root_or_writer_guards(self) -> None:
        self.mutate(
            "UPDATE topics SET execution_scope=? WHERE topic_id=?",
            ("project:example-project", self.topic.topic_id),
        )
        self.refuse_both(cwd=self.other_root)
        self.mutate(
            "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
            (self.session.session_id,),
        )
        self.refuse_both()

    def test_blank_or_oversized_visible_item_is_refused(self) -> None:
        before = self.snapshot()
        for value in ("", " \n\t", "x" * 200_001, None, 123):
            with self.subTest(type=type(value).__name__), self.assertRaises(StateError):
                self.item(value)
            self.assertEqual(self.snapshot(), before)

    def test_oversized_or_nontext_completion_is_refused(self) -> None:
        before = self.snapshot()
        for value in ("x" * 200_001, None, 123):
            with self.subTest(type=type(value).__name__), self.assertRaises(StateError):
                self.complete(value)
            self.assertEqual(self.snapshot(), before)

    def test_visible_item_count_limit_allows_512_and_idempotency_at_capacity(self) -> None:
        for index in range(512):
            self.item("x", f"00000000-0000-4000-8000-{index:012x}")
        before = self.snapshot()
        self.item("x", str(uuid.UUID(int=uuid.UUID(OTHER_UUID).int - 1)))
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(StateError):
            self.item("x", MESSAGE_UUID)
        self.assertEqual(self.snapshot(), before)

    def test_visible_character_limit_is_aggregate_and_allows_exact_capacity(self) -> None:
        self.item("a" * 100_000, OTHER_UUID)
        self.item("b" * 100_000, MESSAGE_UUID)
        before = self.snapshot()
        self.item("b" * 100_000, MESSAGE_UUID)
        self.assertEqual(self.snapshot(), before)
        with self.assertRaises(StateError):
            self.item("c", str(uuid.UUID(int=uuid.UUID(OTHER_UUID).int + 1)))
        self.assertEqual(self.snapshot(), before)

    def test_completion_accepts_exact_text_capacity_and_bounds_partial_recovery(self) -> None:
        text = "c" * 200_000
        self.complete(text)
        checkpoint = self.journal.read(self.job.job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["completed_text"], text)
        self.assertEqual(
            self.journal.partial_text(self.job.job_id),
            "[Earlier partial text omitted]\n" + text[-(MAX_PARTIAL_TEXT - 40) :],
        )

    def test_visible_character_budget_counts_text_after_embedded_nul(self) -> None:
        self.item("a\x00" + "x" * 99_998, OTHER_UUID)
        self.item("b\x00" + "y" * 99_998, MESSAGE_UUID)
        self.assertEqual(sum(len(row[2]) for row in self.visible_rows()), 200_000)
        before = self.snapshot()
        with self.assertRaises(StateError):
            self.item("c", str(uuid.UUID(int=uuid.UUID(OTHER_UUID).int + 1)))
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
