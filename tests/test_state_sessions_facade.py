from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from hermes_codex_router.state import (
    HubState,
    SessionRecord,
    TelegramContractProvenance,
    WriterTransferSnapshot,
)
from hermes_codex_router.state_sessions import (
    SessionRecord as FacadeSessionRecord,
)
from hermes_codex_router.state_sessions import (
    TelegramContractProvenance as FacadeTelegramContractProvenance,
)
from hermes_codex_router.state_sessions import (
    WriterTransferSnapshot as FacadeWriterTransferSnapshot,
)


class SessionsStateFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.state = HubState.open(self.base / "private" / "hub.db")
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=91,
            title="Fictional sessions topic",
        )

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def test_facade_uses_hub_connection_and_preserves_public_record_types(self) -> None:
        self.assertIs(self.state._sessions_state._connection, self.state._connection)
        self.assertEqual(
            self.state._sessions_state._transaction,
            self.state._immediate_transaction,
        )
        self.assertEqual(
            self.state._sessions_state._write_transaction,
            self.state._connection_transaction,
        )
        self.assertIs(SessionRecord, FacadeSessionRecord)
        self.assertIs(WriterTransferSnapshot, FacadeWriterTransferSnapshot)
        self.assertIs(TelegramContractProvenance, FacadeTelegramContractProvenance)

    def test_activation_fault_rolls_back_session_and_topic_owner(self) -> None:
        transaction = self.state._sessions_state._transaction

        @contextmanager
        def fail_after_activation() -> Iterator[None]:
            with transaction():
                yield
                raise RuntimeError("fictional post-activation fault")

        with patch.object(self.state._sessions_state, "_transaction", fail_after_activation):
            with self.assertRaisesRegex(RuntimeError, "post-activation fault"):
                self.state.activate_agent(
                    self.topic.topic_id,
                    "codex",
                    "fictional-model",
                    "high",
                )

        self.assertIsNone(self.state.active_session(self.topic.topic_id))
        self.assertIsNone(self.state.get_topic(self.topic.topic_id).active_agent_id)
        self.assertFalse(self.state._connection.in_transaction)

    def test_simple_session_write_fault_rolls_back_through_hub_owner(self) -> None:
        session = self.state.activate_agent(
            self.topic.topic_id,
            "codex",
            "fictional-model",
            "high",
        )
        transaction = self.state._sessions_state._write_transaction

        @contextmanager
        def fail_after_write() -> Iterator[None]:
            with transaction():
                yield
                raise RuntimeError("fictional post-session-write fault")

        with patch.object(self.state._sessions_state, "_write_transaction", fail_after_write):
            with self.assertRaisesRegex(RuntimeError, "post-session-write fault"):
                self.state.bind_provider_session(
                    session.session_id,
                    "fictional-provider-session",
                    None,
                )

        restored = self.state.get_session(session.session_id)
        self.assertIsNone(restored.provider_session_id)
        self.assertFalse(self.state._connection.in_transaction)

    def test_writer_transfer_fault_restores_telegram_owner(self) -> None:
        session = self.state.activate_agent(
            self.topic.topic_id,
            "codex",
            "fictional-model",
            "high",
        )
        transaction = self.state._sessions_state._transaction

        @contextmanager
        def fail_after_transfer() -> Iterator[None]:
            with transaction():
                yield
                raise RuntimeError("fictional post-writer-transfer fault")

        with patch.object(self.state._sessions_state, "_transaction", fail_after_transfer):
            with self.assertRaisesRegex(RuntimeError, "post-writer-transfer fault"):
                self.state.set_writer_mode(session.session_id, "local")

        self.assertEqual(self.state.get_session(session.session_id).writer_mode, "telegram")
        self.assertFalse(self.state._connection.in_transaction)


if __name__ == "__main__":
    unittest.main()
