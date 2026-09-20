from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Callable

from hermes_codex_router.controller_admission import (
    CommittedAdmission,
    DuplicateAdmission,
    DurableAdmissionFailure,
    DurableAdmissionRequest,
    DurableProviderAdmission,
    RejectedAdmission,
)
from hermes_codex_router.state import HubState, WriterTransferSnapshot
from hermes_codex_router.telegram import (
    DownloadedTelegramFile,
    IncomingAttachment,
    TelegramError,
    TopicMessage,
)


class FakeDownloadTransport:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.downloads: list[str] = []
        self.before_download: Callable[[str], None] | None = None
        self.failure: TelegramError | None = None
        self.unexpected_failure: Exception | None = None

    def download_file(
        self,
        file_id: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> DownloadedTelegramFile:
        self.downloads.append(file_id)
        if self.before_download is not None:
            self.before_download(file_id)
        if self.failure is not None:
            raise self.failure
        if self.unexpected_failure is not None:
            raise self.unexpected_failure
        content = self.files[file_id]
        if len(content) > max_bytes:
            raise AssertionError("test transport received an oversized download")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return DownloadedTelegramFile(
            path=destination,
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )


class DurableAdmissionTests(unittest.TestCase):
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
        self.transport = FakeDownloadTransport({})
        self.admission = DurableProviderAdmission(
            state=self.state,
            telegram=self.transport,
            state_path=self.state_path,
            observer_agent_id="hub",
            message_batch_quiet_ms=1_500,
            message_batch_max_ms=8_000,
        )

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def message(
        self,
        message_id: int,
        text: str = "inspect this request",
        *,
        media_group_id: str | None = None,
        attachments: tuple[IncomingAttachment, ...] = (),
        quote_text: str | None = None,
    ) -> TopicMessage:
        return TopicMessage(
            update_id=message_id,
            message_id=message_id,
            chat_id=self.topic.chat_id,
            thread_id=self.topic.thread_id,
            chat_title=self.topic.title,
            sender_id=42,
            text=text,
            attachments=attachments,
            media_group_id=media_group_id,
            quote_text=quote_text,
        )

    def request(
        self,
        message: TopicMessage,
        *,
        prompt: str | None = None,
        context_watermark: int | None = None,
        handoff_id: str | None = None,
        batchable_user_text: str | None = None,
        take_local_writer: bool = False,
        writer_transfer_snapshot: WriterTransferSnapshot | None = None,
    ) -> DurableAdmissionRequest:
        return DurableAdmissionRequest(
            message=message,
            topic=self.topic,
            session=self.session,
            prompt=message.text if prompt is None else prompt,
            context_watermark=context_watermark,
            handoff_id=handoff_id,
            batchable_user_text=batchable_user_text,
            take_local_writer=take_local_writer,
            writer_transfer_snapshot=writer_transfer_snapshot,
        )

    def jobs(self):
        return self.state.provider_jobs_for_topic(self.topic.topic_id)

    def input_count(self, job_id: str) -> int:
        row = self.state._connection.execute(
            "SELECT COUNT(*) FROM provider_job_inputs WHERE job_id = ?", (job_id,)
        ).fetchone()
        assert row is not None
        return int(row[0])

    def material_count(self, job_id: str) -> int:
        return len(self.state.incoming_materials_for_job(job_id))

    def test_simple_commit_returns_typed_result_and_immutable_job_snapshot(self) -> None:
        result = self.admission.admit(self.request(self.message(101)))

        self.assertIsInstance(result, CommittedAdmission)
        assert isinstance(result, CommittedAdmission)
        self.assertEqual(result.job.message_id, 101)
        self.assertEqual(result.job.agent_id, "codex")
        self.assertEqual(result.job.session_id, self.session.session_id)
        self.assertEqual(result.job.session_generation, self.session.generation)
        self.assertEqual(result.job.model, "gpt-example")
        self.assertEqual(result.job.effort, "high")
        self.assertEqual(result.job.payload_text, "inspect this request")
        self.assertEqual(len(self.jobs()), 1)

    def test_duplicate_observed_input_does_not_download_again(self) -> None:
        self.transport.files["file-one"] = b"fictional-material"
        message = self.message(
            102,
            text="use this file",
            attachments=(
                IncomingAttachment(
                    kind="document",
                    file_id="file-one",
                    file_unique_id="unique-one",
                    file_name="one.txt",
                    mime_type="text/plain",
                    file_size=len(b"fictional-material"),
                ),
            ),
        )

        first = self.admission.admit(self.request(message))
        second = self.admission.admit(self.request(message))

        self.assertIsInstance(first, CommittedAdmission)
        self.assertIsInstance(second, DuplicateAdmission)
        self.assertEqual(self.transport.downloads, ["file-one"])
        self.assertEqual(len(self.jobs()), 1)
        assert isinstance(first, CommittedAdmission)
        self.assertEqual(self.material_count(first.job.job_id), 1)

    def test_over_18k_batch_input_claims_message_without_creating_job(self) -> None:
        message = self.message(103, text="x" * 18_001)

        result = self.admission.admit(self.request(message, batchable_user_text=message.text))

        self.assertIsInstance(result, RejectedAdmission)
        assert isinstance(result, RejectedAdmission)
        self.assertEqual(result.reason, "input_too_long")
        self.assertTrue(self.state.message_already_observed(message.chat_id, message.message_id))
        self.assertEqual(self.jobs(), ())

    def test_over_18k_batch_input_losing_receipt_race_is_duplicate(self) -> None:
        message = self.message(111, text="x" * 18_001)
        original = self.state.claim_message
        self.state.claim_message = lambda *args, **kwargs: False  # type: ignore[method-assign]
        try:
            result = self.admission.admit(self.request(message, batchable_user_text=message.text))
        finally:
            self.state.claim_message = original  # type: ignore[method-assign]

        self.assertIsInstance(result, DuplicateAdmission)
        self.assertEqual(self.jobs(), ())

    def test_material_download_failure_is_retryable_without_receipt_or_job(self) -> None:
        message = self.message(
            104,
            text="download this",
            attachments=(
                IncomingAttachment(
                    kind="document",
                    file_id="file-fails",
                    file_unique_id="unique-fails",
                    file_name="fails.txt",
                    mime_type="text/plain",
                    file_size=10,
                ),
            ),
        )
        self.transport.failure = TelegramError(
            "fictional network timeout",
            operation="download_file",
            failure_class="network_timeout",
        )

        result = self.admission.admit(self.request(message))

        self.assertIsInstance(result, DurableAdmissionFailure)
        assert isinstance(result, DurableAdmissionFailure)
        self.assertEqual(result.reason, "material_download")
        self.assertFalse(self.state.message_already_observed(message.chat_id, message.message_id))
        self.assertEqual(self.jobs(), ())

    def test_unexpected_material_error_is_not_reclassified_as_download_failure(self) -> None:
        message = self.message(
            110,
            text="unexpected material failure",
            attachments=(
                IncomingAttachment(
                    kind="document",
                    file_id="file-unexpected",
                    file_unique_id="unique-unexpected",
                    file_name="unexpected.txt",
                    mime_type="text/plain",
                    file_size=10,
                ),
            ),
        )
        self.transport.files["file-unexpected"] = b"unexpected-marker"
        self.transport.unexpected_failure = RuntimeError("programming fault")

        with self.assertRaisesRegex(RuntimeError, "programming fault"):
            self.admission.admit(self.request(message))

        self.assertFalse(self.state.message_already_observed(message.chat_id, message.message_id))
        self.assertEqual(self.jobs(), ())

    def test_album_holds_tail_before_slow_download_and_shares_one_job(self) -> None:
        self.transport.files.update(
            {
                "album-one": b"album-one-marker",
                "album-two": b"album-two-marker",
            }
        )
        first = self.message(
            105,
            text="compare this album",
            media_group_id="fictional-album",
            attachments=(
                IncomingAttachment(
                    kind="document",
                    file_id="album-one",
                    file_unique_id="album-unique-one",
                    file_name="one.txt",
                    mime_type="text/plain",
                    file_size=16,
                ),
            ),
        )
        second = self.message(
            106,
            text="",
            media_group_id="fictional-album",
            attachments=(
                IncomingAttachment(
                    kind="document",
                    file_id="album-two",
                    file_unique_id="album-unique-two",
                    file_name="two.txt",
                    mime_type="text/plain",
                    file_size=16,
                ),
            ),
        )

        first_result = self.admission.admit(self.request(first, batchable_user_text=first.text))
        self.assertIsInstance(first_result, CommittedAdmission)
        assert isinstance(first_result, CommittedAdmission)
        first_deadline = first_result.job.next_attempt_at
        self.assertIsNotNone(first_deadline)

        hold_seen: list[bool] = []

        def observe_hold(file_id: str) -> None:
            if file_id != "album-two":
                return
            jobs = self.jobs()
            hold_seen.append(
                len(jobs) == 1
                and jobs[0].status == "queued"
                and jobs[0].next_attempt_at is not None
                and jobs[0].next_attempt_at > (first_deadline or "")
            )

        self.transport.before_download = observe_hold
        result = self.admission.admit(
            self.request(second, batchable_user_text="Review the attached Telegram material.")
        )

        self.assertEqual(hold_seen, [True])
        self.assertIsInstance(result, CommittedAdmission)
        self.assertEqual(len(self.jobs()), 1)
        assert isinstance(result, CommittedAdmission)
        self.assertEqual(self.input_count(result.job.job_id), 2)
        self.assertEqual(self.material_count(result.job.job_id), 2)

    def test_material_insert_failure_rolls_back_job_input_receipt_and_material(self) -> None:
        self.transport.files["file-rollback"] = b"rollback-marker"
        message = self.message(
            107,
            text="rollback this admission",
            attachments=(
                IncomingAttachment(
                    kind="document",
                    file_id="file-rollback",
                    file_unique_id="unique-rollback",
                    file_name="rollback.txt",
                    mime_type="text/plain",
                    file_size=15,
                ),
            ),
        )

        original = self.state._insert_incoming_materials

        def fail_insert(*args: object, **kwargs: object) -> None:
            raise sqlite3.IntegrityError("fictional incoming material insert fault")

        self.state._insert_incoming_materials = fail_insert  # type: ignore[method-assign]
        try:
            result = self.admission.admit(self.request(message))
        finally:
            self.state._insert_incoming_materials = original  # type: ignore[method-assign]

        self.assertIsInstance(result, DurableAdmissionFailure)
        assert isinstance(result, DurableAdmissionFailure)
        self.assertEqual(result.reason, "enqueue_uncommitted")
        self.assertFalse(self.state.message_already_observed(message.chat_id, message.message_id))
        self.assertEqual(self.jobs(), ())
        rows = self.state._connection.execute(
            "SELECT COUNT(*) FROM incoming_materials WHERE message_id = ?", (message.message_id,)
        ).fetchone()
        assert rows is not None
        self.assertEqual(int(rows[0]), 0)
        incoming_root = self.state_path.parent / f".{self.state_path.name}.incoming"
        raw_files = tuple(path for path in incoming_root.rglob("*") if path.is_file())
        self.assertTrue(raw_files)

        redelivery = self.admission.admit(self.request(message))
        self.assertIsInstance(redelivery, CommittedAdmission)
        self.assertEqual(self.transport.downloads, ["file-rollback", "file-rollback"])
        self.assertEqual(len(self.jobs()), 1)
        assert isinstance(redelivery, CommittedAdmission)
        self.assertEqual(self.material_count(redelivery.job.job_id), 1)

    def test_file_only_material_uses_bounded_productive_fallback(self) -> None:
        self.transport.files["file-only"] = b"file-only-marker"
        message = self.message(
            108,
            text="",
            attachments=(
                IncomingAttachment(
                    kind="document",
                    file_id="file-only",
                    file_unique_id="unique-only",
                    file_name="only.txt",
                    mime_type="text/plain",
                    file_size=15,
                ),
            ),
        )

        fallback = "Review the attached Telegram material."
        result = self.admission.admit(
            self.request(message, prompt=fallback, batchable_user_text=fallback)
        )

        self.assertIsInstance(result, CommittedAdmission)
        assert isinstance(result, CommittedAdmission)
        self.assertEqual(result.job.payload_text, fallback)
        self.assertEqual(self.material_count(result.job.job_id), 1)

    def test_quote_and_payload_are_bounded_before_durable_commit(self) -> None:
        message = self.message(
            109,
            text="prompt-marker " * 1_400,
            quote_text="quote-marker " * 700,
        )

        result = self.admission.admit(self.request(message))

        self.assertIsInstance(result, CommittedAdmission)
        assert isinstance(result, CommittedAdmission)
        self.assertLessEqual(len(result.job.payload_text), 20_000)
        self.assertIn("quote-marker", result.job.payload_text)


if __name__ == "__main__":
    unittest.main()
