from __future__ import annotations

import hashlib
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from hermes_codex_router.controller_result_publication import (
    PreparedResultPublication,
    PreparedResultPublisher,
    PublishedProviderResult,
)
from hermes_codex_router.incoming_materials import (
    IncomingMaterialDraft,
    PreparedIncomingMaterials,
)
from hermes_codex_router.state import HubState, ProviderJobRecord, StateError
from hermes_codex_router.worker_execution import prepare_worker_artifacts


class PreparedResultPublisherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        self.project_root = base / "project"
        self.project_root.mkdir()
        self.state_path = base / "private" / "hub.db"
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
        self.publisher = PreparedResultPublisher(
            state=self.state,
            state_path=self.state_path,
        )

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def executing_job(self, message_id: int) -> tuple[ProviderJobRecord, Path]:
        raw = self.state_path.parent / f"raw-{message_id}.txt"
        raw.parent.mkdir(parents=True, exist_ok=True)
        content = f"incoming-{message_id}".encode()
        raw.write_bytes(content)
        draft = IncomingMaterialDraft(
            attachment_index=1,
            media_group_id=None,
            kind="document",
            content_kind="text",
            file_unique_id=f"unique-{message_id}",
            display_name=f"input-{message_id}.txt",
            mime_type="text/plain",
            declared_size=len(content),
            storage_path=raw,
            byte_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            status="stored",
        )
        queued, created = self.state.enqueue_provider_job(
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
            materials=(draft,),
        )
        self.assertTrue(created)
        leased = self.state.lease_provider_job("codex", "example-worker")
        assert leased is not None and leased.job_id == queued.job_id
        assert leased.lease_token is not None
        return self.state.mark_provider_job_executing(leased.job_id, leased.lease_token), raw

    def publication(
        self,
        job: ProviderJobRecord,
        raw: Path,
    ) -> PreparedResultPublication:
        artifacts = prepare_worker_artifacts(
            self.project_root,
            job.job_id,
            self.state_path,
            report_rejections=False,
        )
        return PreparedResultPublication(
            job=job,
            project_root=self.project_root,
            prepared_materials=PreparedIncomingMaterials("", (), (), None, (raw,)),
            visible_response="visible result",
            telegram_html="<b>visible result</b>",
            provider_session_id="provider-session",
            actual_model="gpt-actual",
            telegram_contract_version=1,
            artifacts=artifacts.artifacts,
        )

    def stage_artifact(self, job: ProviderJobRecord) -> Path:
        staging = self.project_root / ".hub" / "staging" / job.job_id
        staging.mkdir(parents=True)
        artifact = staging / "report.md"
        artifact.write_text("# Fictional report", encoding="utf-8")
        return artifact

    def test_publish_commits_result_outbox_and_artifact_before_raw_cleanup(self) -> None:
        job, raw = self.executing_job(101)
        self.stage_artifact(job)

        published = self.publisher.publish(self.publication(job, raw))

        self.assertIsInstance(published, PublishedProviderResult)
        self.assertEqual(published.result.job_id, job.job_id)
        self.assertEqual(published.result.visible_response, "visible result")
        self.assertEqual(len(published.artifacts), 1)
        self.assertTrue(published.artifacts[0].path.is_file())
        self.assertEqual(self.state.get_provider_job(job.job_id).status, "result_ready")
        outbox = self.state.get_telegram_outbox_for_job(job.job_id)
        self.assertEqual(outbox.sender_agent_id, "codex")
        self.assertEqual(
            [part.part_type for part in self.state.get_telegram_outbox_parts(outbox.outbox_id)],
            ["text", "document"],
        )
        self.assertEqual(self.state.incoming_materials_for_job(job.job_id)[0].status, "consumed")
        self.assertFalse(raw.exists())

    def test_commit_failure_keeps_job_and_raw_material_for_recovery(self) -> None:
        job, raw = self.executing_job(102)
        self.stage_artifact(job)
        invalid = replace(job, lease_token="wrong-token")

        with self.assertRaisesRegex(StateError, "lease is missing or invalid"):
            self.publisher.publish(self.publication(invalid, raw))

        self.assertEqual(self.state.get_provider_job(job.job_id).status, "executing")
        with self.assertRaisesRegex(StateError, "has no result"):
            self.state.get_provider_result(job.job_id)
        with self.assertRaisesRegex(StateError, "has no Telegram outbox"):
            self.state.get_telegram_outbox_for_job(job.job_id)
        self.assertEqual(self.state.incoming_materials_for_job(job.job_id)[0].status, "stored")
        self.assertTrue(raw.exists())

    def test_artifact_spool_failure_does_not_commit_or_clean_raw_material(self) -> None:
        job, raw = self.executing_job(103)
        self.stage_artifact(job)
        spool = self.state_path.parent / "artifact-spool"
        spool.write_text("not a directory", encoding="utf-8")

        with self.assertRaises(FileExistsError):
            self.publisher.publish(self.publication(job, raw))

        self.assertEqual(self.state.get_provider_job(job.job_id).status, "executing")
        self.assertEqual(self.state.incoming_materials_for_job(job.job_id)[0].status, "stored")
        self.assertTrue(raw.exists())


if __name__ == "__main__":
    unittest.main()
