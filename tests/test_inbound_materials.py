from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.service import QueueAcceptanceError
from hermes_codex_router.telegram import (
    DownloadedTelegramFile,
    parse_topic_message,
)
from tests.fault_matrix_support import FaultMatrixHarness, RecordingAdapter, RecordingBot


class InboundRecordingBot(RecordingBot):
    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__()
        self.files = files
        self.downloads: list[str] = []

    def download_file(
        self,
        file_id: str,
        destination: Path,
        *,
        max_bytes: int,
    ) -> DownloadedTelegramFile:
        self.downloads.append(file_id)
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


def document_update(
    harness: FaultMatrixHarness,
    *,
    message_id: int,
    thread_id: int,
    file_id: str,
    marker_name: str,
    caption: str | None = None,
    media_group_id: str | None = None,
    declared_size: int | None = None,
) -> dict[str, object]:
    update = harness.update(message_id, thread_id, caption or "placeholder")
    message = cast(dict[str, Any], update["message"])
    message.pop("text", None)
    if caption is not None:
        message["caption"] = caption
        message["caption_entities"] = [
            {"type": "mention", "offset": 0, "length": len("@example_opencode_bot")}
        ]
    if media_group_id is not None:
        message["media_group_id"] = media_group_id
    document: dict[str, object] = {
        "file_id": file_id,
        "file_unique_id": f"unique-{file_id}",
        "file_name": marker_name,
        "mime_type": "text/plain",
    }
    if declared_size is not None:
        document["file_size"] = declared_size
    message["document"] = document
    return update


class InboundMaterialTests(unittest.TestCase):
    def test_caption_only_document_is_productive_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            update = document_update(
                harness,
                message_id=501,
                thread_id=901,
                file_id="file-caption",
                marker_name="caption.txt",
                caption="@example_opencode_bot inspect the attached document",
                declared_size=17,
            )

            message = parse_topic_message(cast(dict[str, Any], update))

            self.assertIsNotNone(message)
            assert message is not None
            self.assertEqual(
                message.text,
                "@example_opencode_bot inspect the attached document",
            )
            self.assertEqual(message.text_source, "caption")
            self.assertEqual(len(message.attachments), 1)
            self.assertEqual(message.attachments[0].kind, "document")
            self.assertEqual(message.attachments[0].file_name, "caption.txt")

    def test_album_parts_reach_one_actual_provider_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            controller = harness.controller()
            telegram = InboundRecordingBot(
                {
                    "album-one": b"album-content-marker-one\n",
                    "album-two": b"album-content-marker-two\n",
                }
            )
            controller.telegram = cast(Any, telegram)
            first = document_update(
                harness,
                message_id=511,
                thread_id=911,
                file_id="album-one",
                marker_name="one.txt",
                caption="@example_opencode_bot compare this album",
                media_group_id="fictional-album",
                declared_size=25,
            )
            second = document_update(
                harness,
                message_id=512,
                thread_id=911,
                file_id="album-two",
                marker_name="two.txt",
                media_group_id="fictional-album",
                declared_size=25,
            )
            try:
                self.assertTrue(controller.handle_update(first))
                self.assertTrue(controller.handle_update(second))
                topic = controller.state.find_topic(harness.chat_id, 911)
                assert topic is not None
                jobs = controller.state.provider_jobs_for_topic(topic.topic_id)
                self.assertEqual(len(jobs), 1)
                controller.state.flush_message_batch(topic.topic_id)
            finally:
                controller.close()

            adapter = RecordingAdapter("opencode")
            worker = harness.worker("opencode", adapter)
            try:
                self.assertTrue(worker.run_cycle())
                self.assertEqual(len(adapter.calls), 1)
                provider_input = adapter.calls[0]
                self.assertIn("compare this album", provider_input)
                self.assertIn("album-content-marker-one", provider_input)
                self.assertIn("album-content-marker-two", provider_input)
            finally:
                worker.close()

    def test_album_job_stays_unleased_through_next_poll_and_download(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            controller = harness.controller()
            first_created: datetime | None = None

            class SlowSecondDownloadBot(InboundRecordingBot):
                leased_during_download = False

                def download_file(
                    self,
                    file_id: str,
                    destination: Path,
                    *,
                    max_bytes: int,
                ) -> DownloadedTelegramFile:
                    if file_id == "album-two":
                        assert first_created is not None
                        self.leased_during_download = (
                            controller.state.lease_provider_job(
                                "opencode",
                                "fictional-racing-worker",
                                now=first_created + timedelta(seconds=30),
                            )
                            is not None
                        )
                    return super().download_file(
                        file_id,
                        destination,
                        max_bytes=max_bytes,
                    )

            telegram = SlowSecondDownloadBot(
                {
                    "album-one": b"album-content-marker-one\n",
                    "album-two": b"album-content-marker-two\n",
                }
            )
            controller.telegram = cast(Any, telegram)
            first = document_update(
                harness,
                message_id=513,
                thread_id=912,
                file_id="album-one",
                marker_name="one.txt",
                caption="@example_opencode_bot compare this slow album",
                media_group_id="fictional-slow-album",
                declared_size=25,
            )
            second = document_update(
                harness,
                message_id=514,
                thread_id=912,
                file_id="album-two",
                marker_name="two.txt",
                media_group_id="fictional-slow-album",
                declared_size=25,
            )
            try:
                self.assertTrue(controller.handle_update(first))
                topic = controller.state.find_topic(harness.chat_id, 912)
                assert topic is not None
                first_jobs = controller.state.provider_jobs_for_topic(topic.topic_id)
                self.assertEqual(len(first_jobs), 1)
                first_created = datetime.fromisoformat(first_jobs[0].created_at)
                self.assertIsNone(
                    controller.state.lease_provider_job(
                        "opencode",
                        "fictional-arrival-worker",
                        now=first_created + timedelta(seconds=6),
                    )
                )
                self.assertTrue(controller.handle_update(second))
                self.assertFalse(telegram.leased_during_download)
                self.assertEqual(len(controller.state.provider_jobs_for_topic(topic.topic_id)), 1)
                self.assertEqual(
                    len(
                        controller.state.incoming_materials_for_job(
                            controller.state.provider_jobs_for_topic(topic.topic_id)[0].job_id
                        )
                    ),
                    2,
                )
            finally:
                controller.close()

    def test_attachment_during_active_turn_waits_for_fifo_and_keeps_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            controller = harness.controller()
            telegram = InboundRecordingBot({"late-file": b"late-material-marker\n"})
            controller.telegram = cast(Any, telegram)
            first = harness.update(
                521,
                921,
                "@example_opencode_bot start the fictional long task",
            )
            late = document_update(
                harness,
                message_id=522,
                thread_id=921,
                file_id="late-file",
                marker_name="late.txt",
                caption="@example_opencode_bot use this additional material",
                declared_size=21,
            )
            try:
                self.assertTrue(controller.handle_update(first))
                topic = controller.state.find_topic(harness.chat_id, 921)
                assert topic is not None
                controller.state.flush_message_batch(topic.topic_id)
                parent = controller.state.lease_provider_job("opencode", "active-worker")
                assert parent is not None and parent.lease_token is not None
                parent = controller.state.mark_provider_job_executing(
                    parent.job_id, parent.lease_token
                )

                self.assertTrue(controller.handle_update(late))
                controller.state.flush_message_batch(topic.topic_id)
                self.assertIsNone(
                    controller.state.lease_steer_followup(parent.job_id, "steer-worker")
                )
                controller.state.commit_provider_result(
                    parent.job_id,
                    cast(str, parent.lease_token),
                    visible_response="first task complete",
                    sender_agent_id="opencode",
                    telegram_html="first task complete",
                )
            finally:
                controller.close()

            sender = harness.sender()
            try:
                self.assertTrue(sender.run_cycle())
            finally:
                sender.close()

            adapter = RecordingAdapter("opencode")
            worker = harness.worker("opencode", adapter)
            try:
                self.assertTrue(worker.run_cycle())
                self.assertEqual(len(adapter.calls), 1)
                self.assertIn("late-material-marker", adapter.calls[0])
            finally:
                worker.close()

    def test_oversized_document_is_not_downloaded_and_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            controller = harness.controller()
            telegram = InboundRecordingBot({})
            controller.telegram = cast(Any, telegram)
            update = document_update(
                harness,
                message_id=531,
                thread_id=931,
                file_id="too-large",
                marker_name="large.txt",
                caption="@example_opencode_bot inspect this",
                declared_size=20 * 1024 * 1024 + 1,
            )
            try:
                self.assertTrue(controller.handle_update(update))
                topic = controller.state.find_topic(harness.chat_id, 931)
                assert topic is not None
                controller.state.flush_message_batch(topic.topic_id)
                self.assertEqual(telegram.downloads, [])
            finally:
                controller.close()

            adapter = RecordingAdapter("opencode")
            worker = harness.worker("opencode", adapter)
            try:
                self.assertTrue(worker.run_cycle())
                self.assertIn("20 MB", adapter.calls[0])
            finally:
                worker.close()

            provider_bot = RecordingBot()
            sender = harness.sender(opencode=provider_bot)
            try:
                self.assertTrue(sender.run_cycle())
                self.assertIn("20 MB", provider_bot.sent[0][2])
            finally:
                sender.close()

    def test_external_provider_image_is_explicitly_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            controller = harness.controller()
            telegram = InboundRecordingBot(
                {"photo-file": b"\x89PNG\r\n\x1a\nfictional-image-marker"}
            )
            controller.telegram = cast(Any, telegram)
            update = harness.update(
                536,
                936,
                "@example_opencode_bot inspect the image",
            )
            message = cast(dict[str, Any], update["message"])
            message.pop("text")
            message["caption"] = "@example_opencode_bot inspect the image"
            message["photo"] = [
                {
                    "file_id": "photo-file",
                    "file_unique_id": "unique-photo-file",
                    "file_size": 30,
                    "width": 100,
                    "height": 100,
                }
            ]
            try:
                self.assertTrue(controller.handle_update(update))
                topic = controller.state.find_topic(harness.chat_id, 936)
                assert topic is not None
                controller.state.flush_message_batch(topic.topic_id)
            finally:
                controller.close()

            adapter = RecordingAdapter("opencode")
            worker = harness.worker("opencode", adapter)
            try:
                self.assertTrue(worker.run_cycle())
                self.assertIn("no accepted native image-input contract", adapter.calls[0])
            finally:
                worker.close()

            provider_bot = RecordingBot()
            sender = harness.sender(opencode=provider_bot)
            try:
                self.assertTrue(sender.run_cycle())
                self.assertIn("no accepted native image-input contract", provider_bot.sent[0][2])
            finally:
                sender.close()

    def test_duplicate_document_update_is_not_downloaded_or_invoked_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            controller = harness.controller()
            telegram = InboundRecordingBot({"dedup-file": b"dedup-material-marker\n"})
            controller.telegram = cast(Any, telegram)
            update = document_update(
                harness,
                message_id=541,
                thread_id=941,
                file_id="dedup-file",
                marker_name="dedup.txt",
                caption="@example_opencode_bot inspect once",
                declared_size=22,
            )
            try:
                self.assertTrue(controller.handle_update(update))
                self.assertFalse(controller.handle_update(update))
                topic = controller.state.find_topic(harness.chat_id, 941)
                assert topic is not None
                controller.state.flush_message_batch(topic.topic_id)
                self.assertEqual(telegram.downloads, ["dedup-file"])
                self.assertEqual(len(controller.state.provider_jobs_for_topic(topic.topic_id)), 1)
            finally:
                controller.close()

            adapter = RecordingAdapter("opencode")
            worker = harness.worker("opencode", adapter)
            try:
                self.assertTrue(worker.run_cycle())
                self.assertFalse(worker.run_cycle())
                self.assertEqual(len(adapter.calls), 1)
                self.assertIn("dedup-material-marker", adapter.calls[0])
            finally:
                worker.close()

    def test_enqueue_fault_rolls_back_receipt_and_retry_reuses_one_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            controller = harness.controller()
            telegram = InboundRecordingBot({"retry-file": b"retry-material-marker\n"})
            controller.telegram = cast(Any, telegram)
            update = document_update(
                harness,
                message_id=543,
                thread_id=943,
                file_id="retry-file",
                marker_name="retry.txt",
                caption="@example_opencode_bot retry safely",
                declared_size=22,
            )
            try:
                with patch.object(
                    controller.state,
                    "_insert_incoming_materials",
                    side_effect=sqlite3.IntegrityError("fictional material fault"),
                ):
                    with self.assertRaises(QueueAcceptanceError):
                        controller.handle_update(update)
                topic = controller.state.find_topic(harness.chat_id, 943)
                assert topic is not None
                self.assertFalse(controller.state.message_already_observed(harness.chat_id, 543))
                self.assertEqual(controller.state.provider_jobs_for_topic(topic.topic_id), ())

                self.assertTrue(controller.handle_update(update))
                controller.state.flush_message_batch(topic.topic_id)
                self.assertEqual(len(controller.state.provider_jobs_for_topic(topic.topic_id)), 1)
                self.assertEqual(telegram.downloads, ["retry-file", "retry-file"])
            finally:
                controller.close()

            adapter = RecordingAdapter("opencode")
            worker = harness.worker("opencode", adapter)
            try:
                self.assertTrue(worker.run_cycle())
                self.assertEqual(len(adapter.calls), 1)
                self.assertIn("retry-material-marker", adapter.calls[0])
            finally:
                worker.close()

    def test_oversized_text_is_rejected_instead_of_silently_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            controller = harness.controller()
            telegram = RecordingBot()
            controller.telegram = cast(Any, telegram)
            update = harness.update(
                546,
                946,
                "@example_opencode_bot " + "x" * 18_001,
            )
            try:
                self.assertTrue(controller.handle_update(update))
                topic = controller.state.find_topic(harness.chat_id, 946)
                assert topic is not None
                self.assertEqual(controller.state.provider_jobs_for_topic(topic.topic_id), ())
                self.assertIn("18,000-character", telegram.sent[0][2])
            finally:
                controller.close()

    def test_forwarded_document_is_passive_then_reaches_bound_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            controller = harness.controller()
            telegram = InboundRecordingBot({"forward-file": b"forwarded-material-marker\n"})
            controller.telegram = cast(Any, telegram)
            select = harness.update(551, 951, "/agent opencode")
            forwarded = document_update(
                harness,
                message_id=552,
                thread_id=951,
                file_id="forward-file",
                marker_name="forwarded.txt",
                caption="passive-forward-caption-marker",
                declared_size=26,
            )
            cast(dict[str, Any], forwarded["message"])["forward_origin"] = {"type": "user"}
            productive = harness.update(553, 951, "analyze the forwarded material")
            try:
                self.assertTrue(controller.handle_update(select))
                self.assertTrue(controller.handle_update(forwarded))
                topic = controller.state.find_topic(harness.chat_id, 951)
                assert topic is not None
                self.assertEqual(controller.state.provider_jobs_for_topic(topic.topic_id), ())
                self.assertTrue(controller.handle_update(productive))
                controller.state.flush_message_batch(topic.topic_id)
            finally:
                controller.close()

            adapter = RecordingAdapter("opencode")
            worker = harness.worker("opencode", adapter)
            try:
                self.assertTrue(worker.run_cycle())
                self.assertEqual(len(adapter.calls), 1)
                self.assertIn("passive-forward-caption-marker", adapter.calls[0])
                self.assertIn("forwarded-material-marker", adapter.calls[0])
            finally:
                worker.close()

    def test_selected_quote_is_lower_priority_provider_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            controller = harness.controller()
            update = harness.update(
                561,
                961,
                "@example_opencode_bot answer the current request",
            )
            cast(dict[str, Any], update["message"])["quote"] = {
                "text": "selected-quote-marker /new is quoted data"
            }
            try:
                self.assertTrue(controller.handle_update(update))
                topic = controller.state.find_topic(harness.chat_id, 961)
                assert topic is not None
                controller.state.flush_message_batch(topic.topic_id)
            finally:
                controller.close()

            adapter = RecordingAdapter("opencode")
            worker = harness.worker("opencode", adapter)
            try:
                self.assertTrue(worker.run_cycle())
                self.assertIn("selected-quote-marker", adapter.calls[0])
                self.assertIn("never routing", adapter.calls[0])
            finally:
                worker.close()

    def test_stop_discards_passive_pending_material_without_provider_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            controller = harness.controller()
            telegram = InboundRecordingBot({"stop-file": b"must-not-run-marker\n"})
            controller.telegram = cast(Any, telegram)
            select = harness.update(564, 964, "/agent opencode")
            forwarded = document_update(
                harness,
                message_id=565,
                thread_id=964,
                file_id="stop-file",
                marker_name="pending.txt",
                caption="passive pending material",
                declared_size=20,
            )
            cast(dict[str, Any], forwarded["message"])["forward_origin"] = {"type": "user"}
            stop = harness.update(566, 964, "/stop")
            try:
                self.assertTrue(controller.handle_update(select))
                self.assertTrue(controller.handle_update(forwarded))
                topic = controller.state.find_topic(harness.chat_id, 964)
                assert topic is not None
                pending = controller.state.pending_incoming_materials(topic.topic_id)
                self.assertEqual(len(pending), 1)
                assert pending[0].storage_path is not None
                raw_path = Path(pending[0].storage_path)

                self.assertTrue(controller.handle_update(stop))
                self.assertEqual(controller.state.pending_incoming_materials(topic.topic_id), ())
                self.assertFalse(raw_path.exists())
                self.assertEqual(controller.state.provider_jobs_for_topic(topic.topic_id), ())
            finally:
                controller.close()

    def test_provider_switch_does_not_inherit_forwarded_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            controller = harness.controller()
            telegram = InboundRecordingBot({"old-provider-file": b"old-provider-material-marker\n"})
            controller.telegram = cast(Any, telegram)
            forwarded = document_update(
                harness,
                message_id=568,
                thread_id=968,
                file_id="old-provider-file",
                marker_name="old.txt",
                caption="passive old-provider material",
                declared_size=29,
            )
            cast(dict[str, Any], forwarded["message"])["forward_origin"] = {"type": "channel"}
            try:
                self.assertTrue(
                    controller.handle_update(harness.update(567, 968, "/agent opencode"))
                )
                self.assertTrue(controller.handle_update(forwarded))
                topic = controller.state.find_topic(harness.chat_id, 968)
                assert topic is not None
                pending = controller.state.pending_incoming_materials(topic.topic_id)
                self.assertEqual(len(pending), 1)
                assert pending[0].storage_path is not None
                raw_path = Path(pending[0].storage_path)

                self.assertTrue(
                    controller.handle_update(harness.update(569, 968, "/agent antigravity"))
                )
                self.assertTrue(
                    controller.handle_update(harness.update(570, 968, "new provider task"))
                )
                controller.state.flush_message_batch(topic.topic_id)
                self.assertEqual(controller.state.pending_incoming_materials(topic.topic_id), ())
                self.assertFalse(raw_path.exists())
            finally:
                controller.close()

            adapter = RecordingAdapter("antigravity")
            worker = harness.worker("antigravity", adapter)
            try:
                self.assertTrue(worker.run_cycle())
                self.assertNotIn("old-provider-material-marker", adapter.calls[0])
            finally:
                worker.close()

    def test_next_update_removes_consumed_raw_file_left_by_crash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            controller = harness.controller()
            telegram = InboundRecordingBot({"recovery-file": b"recovery-material-marker\n"})
            controller.telegram = cast(Any, telegram)
            update = document_update(
                harness,
                message_id=571,
                thread_id=971,
                file_id="recovery-file",
                marker_name="recovery.txt",
                caption="@example_opencode_bot inspect before recovery",
                declared_size=25,
            )
            try:
                self.assertTrue(controller.handle_update(update))
                topic = controller.state.find_topic(harness.chat_id, 971)
                assert topic is not None
                controller.state.flush_message_batch(topic.topic_id)
                job = controller.state.provider_jobs_for_topic(topic.topic_id)[0]
                material = controller.state.incoming_materials_for_job(job.job_id)[0]
                assert material.storage_path is not None
                raw_path = Path(material.storage_path)
                self.assertTrue(raw_path.exists())

                leased = controller.state.lease_provider_job("opencode", "recovery-worker")
                assert leased is not None and leased.lease_token is not None
                executing = controller.state.mark_provider_job_executing(
                    leased.job_id, leased.lease_token
                )
                controller.state.commit_provider_result(
                    executing.job_id,
                    cast(str, executing.lease_token),
                    visible_response="result committed before fictional crash",
                    sender_agent_id="opencode",
                    telegram_html="result committed before fictional crash",
                )
                consumed = controller.state.incoming_materials_for_job(job.job_id)[0]
                self.assertEqual(consumed.status, "consumed")
                self.assertTrue(raw_path.exists())

                self.assertTrue(
                    controller.handle_update(
                        harness.update(
                            572,
                            971,
                            "@example_opencode_bot continue after recovery",
                        )
                    )
                )
                recovered = controller.state.incoming_materials_for_job(job.job_id)[0]
                self.assertEqual(recovered.status, "consumed")
                self.assertFalse(raw_path.exists())
            finally:
                controller.close()

    def test_symlink_tampering_fails_before_provider_invocation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            controller = harness.controller()
            telegram = InboundRecordingBot({"tamper-file": b"tamper-material-marker\n"})
            controller.telegram = cast(Any, telegram)
            update = document_update(
                harness,
                message_id=571,
                thread_id=971,
                file_id="tamper-file",
                marker_name="tamper.txt",
                caption="@example_opencode_bot inspect safely",
                declared_size=23,
            )
            try:
                self.assertTrue(controller.handle_update(update))
                topic = controller.state.find_topic(harness.chat_id, 971)
                assert topic is not None
                controller.state.flush_message_batch(topic.topic_id)
                job = controller.state.provider_jobs_for_topic(topic.topic_id)[0]
                material = controller.state.incoming_materials_for_job(job.job_id)[0]
                assert material.storage_path is not None
                stored = Path(material.storage_path)
                replacement = stored.with_name("replacement")
                replacement.write_bytes(b"tamper-material-marker\n")
                stored.unlink()
                stored.symlink_to(replacement)
            finally:
                controller.close()

            adapter = RecordingAdapter("opencode")
            worker = harness.worker("opencode", adapter)
            try:
                self.assertTrue(worker.run_cycle())
                self.assertEqual(adapter.calls, [])
                self.assertEqual(worker.state.get_provider_job(job.job_id).status, "failed")
            finally:
                worker.close()


if __name__ == "__main__":
    unittest.main()
