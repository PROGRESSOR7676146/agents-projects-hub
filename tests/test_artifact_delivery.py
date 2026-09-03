from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from hermes_codex_router.artifact_delivery import deliver_staged_artifacts_immediately


class FakeTelegram:
    def __init__(self) -> None:
        self.documents: list[tuple[str, bytes]] = []
        self.messages: list[str] = []

    def send_html(self, chat_id: int, thread_id: int, html: str) -> int:
        del chat_id, thread_id
        self.messages.append(html)
        return 1

    def send_document(
        self,
        chat_id: int,
        thread_id: int,
        document_path: Path,
        *,
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> int:
        del chat_id, thread_id, caption, reply_markup, mime_type
        self.documents.append((str(file_name), document_path.read_bytes()))
        return 2


class ImmediateArtifactDeliveryTests(unittest.TestCase):
    def test_snapshots_sends_and_removes_valid_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = project / ".hub" / "staging" / "direct-job"
            staging.mkdir(parents=True)
            (staging / "result.md").write_text("done\n", encoding="utf-8")
            state_path = root / "state" / "state.db"
            state_path.parent.mkdir()
            telegram = FakeTelegram()

            rejected = deliver_staged_artifacts_immediately(
                telegram,
                chat_id=1,
                thread_id=1,
                project_root=project,
                state_path=state_path,
                job_id="direct-job",
            )

            self.assertEqual(rejected, ())
            self.assertEqual(telegram.documents, [("result.md", b"done\n")])
            self.assertFalse(staging.exists())
            self.assertEqual(list((state_path.parent / "artifact-spool").rglob("*.md")), [])

    def test_reports_rejected_file_without_blocking_valid_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = root / "project"
            staging = project / ".hub" / "staging" / "direct-job"
            staging.mkdir(parents=True)
            (staging / "answer.txt").write_text("ok", encoding="utf-8")
            (staging / "access-token.txt").write_text("not-a-real-secret", encoding="utf-8")
            state_path = root / "state" / "state.db"
            state_path.parent.mkdir()
            telegram = FakeTelegram()

            rejected = deliver_staged_artifacts_immediately(
                telegram,
                chat_id=1,
                thread_id=1,
                project_root=project,
                state_path=state_path,
                job_id="direct-job",
            )

            self.assertEqual(len(rejected), 1)
            self.assertEqual(telegram.documents, [("answer.txt", b"ok")])
            self.assertIn("Not attached", telegram.messages[0])


if __name__ == "__main__":
    unittest.main()
