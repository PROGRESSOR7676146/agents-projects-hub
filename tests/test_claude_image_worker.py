from __future__ import annotations

import hashlib
import subprocess
import sys
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.external_runtime import ExternalCliAdapter
from hermes_codex_router.incoming_materials import IncomingMaterialDraft, incoming_storage_root
from hermes_codex_router.root_blockers import persistent_root_blocker
from tests import test_claude_native_worker as fixtures
from tests.delivery_fixture import complete_final_delivery
from tests.test_claude_image_input import JPEG, PNG


class ClaudeImageWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ClaudeNativeWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.fixture.config = replace(self.fixture.fixture.config, claude_image_input=True)
        self.adapter = fixtures.ObservingClaudeAdapter(self.fixture.path)
        self.worker = self.fixture.worker(self.adapter)

    def enqueue(
        self,
        message_id: int,
        contents: tuple[bytes, ...] = (PNG, JPEG),
        *,
        unavailable: tuple[str, ...] = (),
    ) -> str:
        state = self.worker.state
        topic = state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=77,
            title="Example",
            execution_root=self.fixture.root,
        )
        session = state.activate_agent(topic.topic_id, "claude", "example-model", "high")
        materials = []
        for index, data in enumerate(contents, 1):
            path = incoming_storage_root(self.fixture.path) / "aa" / f"example-{message_id}-{index}"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            materials.append(
                IncomingMaterialDraft(
                    attachment_index=index,
                    media_group_id="example-album",
                    kind="document",
                    content_kind="image",
                    file_unique_id=None,
                    display_name=f"image-{index}.png",
                    mime_type="image/png" if data.startswith(b"\x89PNG") else "image/jpeg",
                    declared_size=len(data),
                    storage_path=path,
                    byte_size=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    status="stored",
                )
            )
        for index, detail in enumerate(unavailable, len(materials) + 1):
            materials.append(
                IncomingMaterialDraft(
                    attachment_index=index,
                    media_group_id="example-album",
                    kind="document",
                    content_kind=None,
                    file_unique_id=None,
                    display_name=f"excluded-{index}.gif",
                    mime_type="image/gif",
                    declared_size=None,
                    storage_path=None,
                    byte_size=None,
                    sha256=None,
                    status="unavailable",
                    unavailable_code="unsupported_format",
                    unavailable_detail=detail,
                )
            )
        job, _ = state.enqueue_provider_job(
            idempotency_key=f"example-images:{message_id}",
            chat_id=topic.chat_id,
            message_id=message_id,
            topic_id=topic.topic_id,
            agent_id=session.agent_id,
            session_id=session.session_id,
            session_generation=session.generation,
            provider_session_id=session.provider_session_id,
            model=session.model,
            effort=session.effort,
            payload_text=f"Example caption {message_id}",
            materials=materials,
        )
        return job.job_id

    def deliver(self) -> None:
        state = self.worker.state
        delivery = state.lease_telegram_outbox("claude", "example-sender")
        assert delivery is not None and delivery.lease_token is not None
        complete_final_delivery(
            state, delivery.outbox_id, delivery.lease_token, telegram_message_id=501
        )

    def test_album_and_next_turn_keep_exact_bytes_new_materials_and_native_uuid(self) -> None:
        first = self.enqueue(1)
        self.assertTrue(self.worker.run_cycle())
        call = self.adapter.calls[0]
        native = self.fixture.assert_binding_visible_before_invocation(self.adapter)
        self.assertEqual([part.data for part in call["claude_images"]], [PNG, JPEG])
        self.assertIn("Example caption 1", call["prompt"])
        self.assertEqual(call["model"], "example-model")
        self.assertEqual(call["effort"], "high")
        self.assertEqual(
            {part.status for part in self.worker.state.incoming_materials_for_job(first)},
            {"consumed"},
        )
        self.deliver()
        second = self.enqueue(2, (JPEG,))
        self.assertTrue(self.worker.run_cycle())
        self.assertEqual(self.adapter.calls[1]["session_id"], native)
        self.assertIsNone(self.adapter.calls[1]["new_session_id"])
        self.assertEqual([part.data for part in self.adapter.calls[1]["claude_images"]], [JPEG])
        self.assertEqual(self.worker.state.get_provider_job(second).status, "result_ready")
        self.assertNotIn("Example caption 1", self.adapter.calls[1]["prompt"])

    def test_disabled_gate_names_unavailable_images_in_prompt_and_saved_final(self) -> None:
        self.worker.config = replace(self.worker.config, claude_image_input=False)
        job = self.enqueue(1)
        self.assertTrue(self.worker.run_cycle())
        self.assertEqual(self.adapter.calls[0]["claude_images"], ())
        self.assertIn("UNAVAILABLE", self.adapter.calls[0]["prompt"])
        result = self.worker.state.get_provider_result(job)
        self.assertIn("Incoming material unavailable", result.visible_response)

    def test_ambiguous_native_failure_retains_material_and_root_without_replay(self) -> None:
        self.adapter.outcome = "contradictory"
        job = self.enqueue(1)
        records = self.worker.state.incoming_materials_for_job(job)
        self.assertTrue(self.worker.run_cycle())
        self.fixture.assert_uncertain_and_not_replayed(self.worker, self.adapter, job)
        self.assertEqual(
            {part.status for part in self.worker.state.incoming_materials_for_job(job)}, {"stored"}
        )
        for part in records:
            assert part.storage_path is not None
            self.assertTrue(Path(part.storage_path).is_file())
        self.assertIsNotNone(
            persistent_root_blocker(self.worker.state._connection, topic_id=records[0].topic_id)
        )
        checkpoint = ExecutionJournal(self.worker.state).read(job)
        assert checkpoint is not None
        self.assertIsNone(checkpoint["completed_text"])

    def test_integrity_failure_never_calls_provider(self) -> None:
        job = self.enqueue(1)
        record = self.worker.state.incoming_materials_for_job(job)[0]
        assert record.storage_path is not None
        Path(record.storage_path).write_bytes(b"changed")
        self.assertTrue(self.worker.run_cycle())
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.worker.state.get_provider_job(job).status, "failed")

    def test_owned_public_worker_rejects_missing_or_downgraded_ack_without_replay(self) -> None:
        for downgrade in (False, True):
            with self.subTest(downgrade=downgrade):
                fixture = ClaudeImageWorkerTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                adapter = ExternalCliAdapter("claude")
                fixture.worker.adapter = adapter
                job = fixture.enqueue(10 + int(downgrade))
                original_records = fixture.worker.state.incoming_materials_for_job(job)
                script = "import json,sys;frame=json.loads(sys.stdin.buffer.read());native=frame['session_id'];"
                if downgrade:
                    script += "frame['isReplay']=True;frame['message']['content']=[{'type':'text','text':'Example downgrade'}];print(json.dumps(frame),flush=True);"
                script += "print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':native,'result':'Must not be saved'}),flush=True)"
                argv = (sys.executable, "-I", "-c", script)
                original_spawn = subprocess.Popen
                children = []

                def spawn(*args, **kwargs):
                    child = original_spawn(*args, **kwargs)
                    if args[0] == argv:
                        children.append(child)
                    return child

                with (
                    patch.object(adapter, "_verified_claude_argv", return_value=argv),
                    patch("subprocess.Popen", side_effect=spawn),
                    patch.dict(
                        "os.environ",
                        {
                            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                            "ANTHROPIC_AUTH_TOKEN": "example",
                        },
                        clear=True,
                    ),
                ):
                    self.assertTrue(fixture.worker.run_cycle())
                    self.assertFalse(fixture.worker.run_cycle())
                state = fixture.worker.state
                self.assertEqual(state.get_provider_job(job).status, "indeterminate")
                fixture.fixture.assert_no_result(state, job)
                checkpoint = ExecutionJournal(state).read(job)
                assert checkpoint is not None
                self.assertIsNone(checkpoint["completed_text"])
                self.assertIsNotNone(
                    persistent_root_blocker(
                        state._connection, topic_id=original_records[0].topic_id
                    )
                )
                self.assertEqual(
                    {record.status for record in state.incoming_materials_for_job(job)}, {"stored"}
                )
                for record in original_records:
                    assert record.storage_path is not None
                    self.assertTrue(Path(record.storage_path).is_file())
                self.assertEqual(len(children), 1)
                self.assertIsNotNone(children[0].returncode)
                self.assertIsNone(adapter._active_process)
                for pipe in (children[0].stdin, children[0].stdout, children[0].stderr):
                    assert pipe is not None
                    self.assertTrue(pipe.closed)

    def test_completion_delivery_fault_recovers_original_material_notices_without_replay(
        self,
    ) -> None:
        oversized = PNG + b"x" * (2 * 1024 * 1024)
        job = self.enqueue(1, (PNG, oversized), unavailable=("GIF images are unsupported",))
        with patch(
            "hermes_codex_router.external_worker.prepare_worker_artifacts",
            side_effect=RuntimeError("Example delivery preparation fault"),
        ):
            self.assertTrue(self.worker.run_cycle())
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.worker.state.get_provider_job(job).status, "result_ready")
        result = self.worker.state.get_provider_result(job)
        self.assertIn("GIF images are unsupported", result.visible_response)
        self.assertIn("2 MiB", result.visible_response)
        self.assertEqual(result.visible_response.count("Incoming material unavailable"), 1)
        checkpoint = ExecutionJournal(self.worker.state).read(job)
        assert checkpoint is not None
        self.assertEqual(checkpoint["completed_text"], "Fictional visible answer")
        self.assertIn(checkpoint["claude_material_notice"], result.visible_response)
        self.assertFalse(self.worker.run_cycle())
        self.assertEqual(len(self.adapter.calls), 1)

    def test_over_limit_notice_refuses_before_native_identity_or_invocation(self) -> None:
        job = self.enqueue(1, (), unavailable=tuple("Example " + "x" * 480 for _ in range(90)))
        self.assertTrue(self.worker.run_cycle())
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(self.worker.state.get_provider_job(job).status, "failed")
        self.assertIsNone(ExecutionJournal(self.worker.state).read(job))

    def test_expired_completion_retains_original_notice_after_configuration_change(self) -> None:
        job = self.enqueue(1, (PNG,), unavailable=("GIF images are unsupported",))
        with (
            patch(
                "hermes_codex_router.external_worker.prepare_worker_artifacts",
                side_effect=RuntimeError("Example preparation fault"),
            ),
            patch(
                "hermes_codex_router.external_worker.recover_claude_completion", return_value=True
            ),
        ):
            self.assertTrue(self.worker.run_cycle())
        saved = self.worker.state.get_provider_job(job)
        self.assertEqual(saved.status, "executing")
        assert saved.lease_token is not None
        self.worker.state.heartbeat_provider_job(
            job,
            saved.lease_token,
            lease_seconds=1,
            now=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        self.worker.config = replace(self.worker.config, claude_image_input=False)
        self.assertTrue(self.worker.run_cycle())
        visible = self.worker.state.get_provider_result(job).visible_response
        self.assertIn("GIF images are unsupported", visible)
        self.assertNotIn("has no accepted native image-input", visible)
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertFalse(self.worker.run_cycle())


if __name__ == "__main__":
    unittest.main()
