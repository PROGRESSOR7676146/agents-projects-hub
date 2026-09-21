from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.incoming_materials import IncomingMaterialDraft
from hermes_codex_router.state import HubState, StateError


class IncomingMaterialsStateFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tempdir.name) / "private" / "hub.db"
        self.state = HubState.open(self.state_path)
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=77,
            title="Example topic",
        )
        self.session = self.state.activate_agent(
            self.topic.topic_id, "codex", "gpt-example", "high"
        )

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def unavailable_draft(self) -> IncomingMaterialDraft:
        return IncomingMaterialDraft(
            attachment_index=1,
            media_group_id=None,
            kind="document",
            content_kind=None,
            file_unique_id="fictional-unavailable",
            display_name="unavailable.txt",
            mime_type="text/plain",
            declared_size=10,
            storage_path=None,
            byte_size=None,
            sha256=None,
            status="unavailable",
            unavailable_code="unsupported",
            unavailable_detail="Fictional unsupported material",
        )

    def stored_draft(self, message_id: int) -> tuple[IncomingMaterialDraft, Path]:
        raw = self.state_path.parent / f"raw-{message_id}.txt"
        raw.parent.mkdir(parents=True, exist_ok=True)
        content = f"fictional-{message_id}".encode()
        raw.write_bytes(content)
        return (
            IncomingMaterialDraft(
                attachment_index=1,
                media_group_id=None,
                kind="document",
                content_kind="text",
                file_unique_id=f"fictional-{message_id}",
                display_name=f"input-{message_id}.txt",
                mime_type="text/plain",
                declared_size=len(content),
                storage_path=raw,
                byte_size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                status="stored",
            ),
            raw,
        )

    def enqueue(self, message_id: int, material: IncomingMaterialDraft):
        return self.state.enqueue_provider_job(
            idempotency_key=f"telegram:{self.topic.chat_id}:{message_id}",
            chat_id=self.topic.chat_id,
            message_id=message_id,
            topic_id=self.topic.topic_id,
            agent_id=self.session.agent_id,
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model=self.session.model,
            effort=self.session.effort,
            payload_text=f"request-{message_id}",
            materials=(material,),
        )

    def test_facade_uses_hub_state_connection_and_public_forward_contract(self) -> None:
        self.assertIs(
            self.state._incoming_material_state._connection,
            self.state._connection,
        )
        self.assertTrue(
            self.state.record_forwarded_quote(
                topic_id=self.topic.topic_id,
                chat_id=self.topic.chat_id,
                message_id=101,
                observer_agent_id="hub",
                text="Fictional forwarded quote",
                materials=(self.unavailable_draft(),),
                session=self.session,
            )
        )

        pending = self.state.pending_incoming_materials(self.topic.topic_id)
        self.assertEqual(len(pending), 1)
        self.assertEqual((pending[0].origin, pending[0].job_id), ("forward", None))
        self.assertEqual(
            self.state.delete_pending_incoming_materials(
                self.topic.topic_id, (pending[0].material_id,)
            ),
            1,
        )
        self.assertEqual(self.state.pending_incoming_materials(self.topic.topic_id), ())

    def test_failure_after_facade_insert_rolls_back_entire_admission(self) -> None:
        original = self.state._incoming_material_state.insert

        def fail_after_insert(**kwargs: object) -> None:
            original(**kwargs)  # type: ignore[arg-type]
            raise RuntimeError("fictional post-material fault")

        self.state._incoming_material_state.insert = fail_after_insert  # type: ignore[method-assign]
        try:
            with self.assertRaisesRegex(RuntimeError, "post-material fault"):
                self.enqueue(102, self.unavailable_draft())
        finally:
            self.state._incoming_material_state.insert = original  # type: ignore[method-assign]

        self.assertEqual(self.state.provider_jobs_for_topic(self.topic.topic_id), ())
        self.assertFalse(self.state.message_already_observed(self.topic.chat_id, 102))
        count = self.state._connection.execute(
            "SELECT COUNT(*) FROM incoming_materials WHERE message_id = 102"
        ).fetchone()
        assert count is not None
        self.assertEqual(int(count[0]), 0)

    def test_late_result_fault_rolls_back_material_consumption_and_publication(self) -> None:
        draft, raw = self.stored_draft(103)
        queued, created = self.enqueue(103, draft)
        self.assertTrue(created)
        leased = self.state.lease_provider_job("codex", "example-worker")
        assert leased is not None and leased.job_id == queued.job_id
        assert leased.lease_token is not None
        executing = self.state.mark_provider_job_executing(leased.job_id, leased.lease_token)
        assert executing.lease_token is not None
        original = self.state._insert_telegram_outbox_parts

        def fail_outbox_parts(*args: object, **kwargs: object) -> None:
            raise RuntimeError("fictional late result fault")

        self.state._insert_telegram_outbox_parts = fail_outbox_parts  # type: ignore[method-assign]
        try:
            with self.assertRaisesRegex(RuntimeError, "late result fault"):
                self.state.commit_provider_result(
                    executing.job_id,
                    executing.lease_token,
                    visible_response="fictional result",
                    sender_agent_id="codex",
                    telegram_html="fictional result",
                )
        finally:
            self.state._insert_telegram_outbox_parts = original  # type: ignore[method-assign]

        self.assertEqual(self.state.get_provider_job(executing.job_id).status, "executing")
        self.assertEqual(
            self.state.incoming_materials_for_job(executing.job_id)[0].status,
            "stored",
        )
        self.assertTrue(raw.exists())
        with self.assertRaisesRegex(StateError, "has no result"):
            self.state.get_provider_result(executing.job_id)
        with self.assertRaisesRegex(StateError, "has no Telegram outbox"):
            self.state.get_telegram_outbox_for_job(executing.job_id)


if __name__ == "__main__":
    unittest.main()
