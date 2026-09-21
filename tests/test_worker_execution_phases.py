from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from hermes_codex_router.codex_appserver import CodexThread, RateLimits, TurnResult
from hermes_codex_router.codex_failure import CodexPreparationError
from hermes_codex_router.external_runtime import (
    ExternalTurnResult,
    ProviderLimitError,
    ProviderUnavailableError,
)
from hermes_codex_router.incoming_materials import (
    IncomingMaterialError,
    PreparedIncomingMaterials,
)
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.project_resolution import ResolvedProject
from hermes_codex_router.provider_limits import ProviderLimit
from hermes_codex_router.registry import ExecutionRootError
from hermes_codex_router.state import ProviderJobRecord, TopicRecord
from hermes_codex_router.worker_execution import (
    ProviderTurnStopped,
    WorkerExecutionTarget,
    classify_worker_failure,
    codex_provider_prompt,
    codex_turn_text,
    external_provider_prompt,
    invoke_external_provider_turn,
    open_codex_provider_thread,
    prepare_codex_worker_result,
    prepare_external_worker_result,
    prepare_worker_artifacts,
    prepare_worker_materials,
    prepare_worker_staging_directory,
    require_provider_job_lease,
    resolve_embedded_worker_target,
    resolve_external_worker_target,
    revalidate_worker_execution_root,
    start_codex_provider_turn,
    wait_for_codex_provider_turn,
    worker_needs_full_telegram_contract,
)


class WorkerExecutionPhaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.project = Project(
            "example-project",
            "Example Project",
            "Example Topic",
            Path("/home/example/project"),
        )
        self.registry = ProjectRegistry(
            1,
            (Path("/home/example"),),
            (self.project,),
        )
        self.topic = TopicRecord(
            41,
            self.project.project_id,
            -1001234567890,
            77,
            "Fictional worker topic",
            "codex",
            f"root:{self.project.root}",
        )

    def job(
        self,
        lease_token: str | None = "fictional-lease",
        *,
        provider_session_id: str | None = "fictional-provider-session",
    ) -> ProviderJobRecord:
        return cast(
            ProviderJobRecord,
            SimpleNamespace(
                job_id="fictional-job",
                topic_id=self.topic.topic_id,
                lease_token=lease_token,
                provider_session_id=provider_session_id,
                session_id="fictional-session",
                payload_text="Fictional request",
                model="fictional-model",
                effort="high",
            ),
        )

    def test_lease_validation_returns_capability_and_uses_caller_error(self) -> None:
        self.assertEqual(
            require_provider_job_lease(self.job(), error_factory=ValueError),
            "fictional-lease",
        )
        with self.assertRaisesRegex(ValueError, "leased provider job has no lease token"):
            require_provider_job_lease(self.job(None), error_factory=ValueError)

    def test_external_target_refreshes_exact_topic_binding(self) -> None:
        state = Mock()
        state.get_topic.return_value = self.topic
        config = cast(Any, object())
        resolved = ResolvedProject(
            self.project,
            self.registry,
            self.topic.chat_id,
            "static",
            None,
        )
        with patch(
            "hermes_codex_router.worker_execution.resolve_project_context",
            return_value=resolved,
        ) as resolver:
            target = resolve_external_worker_target(config, state, self.job())

        self.assertEqual(target, WorkerExecutionTarget(self.registry, self.project, self.topic))
        state.get_topic.assert_called_once_with(self.topic.topic_id)
        resolver.assert_called_once_with(
            config,
            state,
            chat_id=self.topic.chat_id,
            expected_project_id=self.topic.project_id,
        )

    def test_embedded_target_requires_registered_project(self) -> None:
        state = Mock()
        state.get_topic.return_value = self.topic
        target = resolve_embedded_worker_target(state, self.registry, self.job())
        self.assertEqual(target, WorkerExecutionTarget(self.registry, self.project, self.topic))

    def test_execution_root_revalidation_preserves_identity_and_replaces_only_root(self) -> None:
        target = WorkerExecutionTarget(self.registry, self.project, self.topic)
        state = Mock()
        lane_root = Path("/home/example/project-lane")
        with patch(
            "hermes_codex_router.worker_execution.resolve_topic_execution_root",
            return_value=lane_root,
        ) as resolver:
            refreshed = revalidate_worker_execution_root(state, target)

        resolver.assert_called_once_with(state, self.registry, self.topic)
        self.assertEqual(refreshed.registry, target.registry)
        self.assertEqual(refreshed.topic, target.topic)
        self.assertEqual(refreshed.project.root, lane_root)
        self.assertEqual(target.project.root, Path("/home/example/project"))

    def test_material_preparation_uses_persisted_job_materials_and_exact_boundary(self) -> None:
        state = Mock()
        records = (object(),)
        state.incoming_materials_for_job.return_value = records
        prepared = PreparedIncomingMaterials(" suffix", (), (), None, ())
        with patch(
            "hermes_codex_router.worker_execution.prepare_incoming_materials",
            return_value=prepared,
        ) as materializer:
            result = prepare_worker_materials(
                state,
                state_path=Path("/home/example/private/hub.db"),
                execution_root=self.project.root,
                job=self.job(),
                runtime="codex",
            )

        self.assertIs(result, prepared)
        state.incoming_materials_for_job.assert_called_once_with("fictional-job")
        materializer.assert_called_once_with(
            records,
            state_path=Path("/home/example/private/hub.db"),
            execution_root=self.project.root,
            job_id="fictional-job",
            runtime="codex",
        )

    def test_staging_and_contract_decision_are_bounded_pre_invocation_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            staging = prepare_worker_staging_directory(root, "fictional-job")
            self.assertEqual(staging, root / ".hub" / "staging" / "fictional-job")
            self.assertTrue(staging.is_dir())

        state = Mock()
        state.telegram_contract_version.return_value = 4
        with patch(
            "hermes_codex_router.worker_execution.telegram_contract_version",
            return_value=5,
        ):
            self.assertTrue(worker_needs_full_telegram_contract(state, self.job(), "codex"))
        state.telegram_contract_version.return_value = 5
        with patch(
            "hermes_codex_router.worker_execution.telegram_contract_version",
            return_value=5,
        ):
            self.assertFalse(worker_needs_full_telegram_contract(state, self.job(), "codex"))
        self.assertTrue(
            worker_needs_full_telegram_contract(
                state,
                self.job(provider_session_id=None),
                "codex",
            )
        )

    def test_provider_prompt_construction_preserves_material_and_fallback_context(self) -> None:
        prepared = PreparedIncomingMaterials("\nMATERIAL", (), (), None, ())
        current = codex_turn_text(self.job(), prepared)
        self.assertEqual(current, "Fictional request\nMATERIAL")
        bridged = codex_turn_text(
            self.job(),
            prepared,
            fallback_visible_context="Fictional previous context",
        )
        self.assertIn("PREVIOUS VISIBLE CONTEXT:\nFictional previous context", bridged)
        self.assertIn("CURRENT USER MESSAGE:\nFictional request\nMATERIAL", bridged)

        staging = Path("/home/example/project/.hub/staging/fictional-job")
        with patch(
            "hermes_codex_router.worker_execution.telegram_user_turn_prompt",
            return_value="codex-prompt",
        ) as codex_builder:
            self.assertEqual(
                codex_provider_prompt(current, staging_dir=staging),
                "codex-prompt",
            )
        codex_builder.assert_called_once_with(current, staging_dir=staging)

        with patch(
            "hermes_codex_router.worker_execution.telegram_turn_prompt",
            return_value="external-prompt",
        ) as external_builder:
            self.assertEqual(
                external_provider_prompt(
                    self.job(),
                    prepared,
                    runtime="opencode",
                    full_contract=True,
                    staging_dir=staging,
                ),
                "external-prompt",
            )
        external_builder.assert_called_once_with(
            "Fictional request\nMATERIAL",
            runtime="opencode",
            new_session=True,
            staging_dir=staging,
        )

    def test_codex_thread_open_preserves_resume_and_forced_new_boundaries(self) -> None:
        client = Mock()
        resumed = CodexThread(
            "fictional-provider-session",
            self.project.root,
            "fictional-model",
            "openai",
        )
        started = CodexThread(
            "fictional-new-session",
            self.project.root,
            "fictional-model",
            "openai",
        )
        client.resume_thread.return_value = resumed
        client.start_thread.return_value = started

        self.assertIs(
            open_codex_provider_thread(
                client,
                self.job(),
                self.project,
                developer_instructions="Fictional contract",
            ),
            resumed,
        )
        client.resume_thread.assert_called_once_with(
            thread_id="fictional-provider-session",
            cwd=self.project.root,
            model="fictional-model",
            developer_instructions="Fictional contract",
        )
        client.start_thread.assert_not_called()

        self.assertIs(
            open_codex_provider_thread(
                client,
                self.job(),
                self.project,
                developer_instructions="Fictional contract",
                force_new_thread=True,
            ),
            started,
        )
        client.start_thread.assert_called_once_with(
            cwd=self.project.root,
            model="fictional-model",
            project_id=self.project.project_id,
            developer_instructions="Fictional contract",
        )

    def test_codex_turn_start_and_completion_are_separate_invocation_boundaries(self) -> None:
        client = Mock()
        thread = CodexThread(
            "fictional-thread",
            self.project.root,
            "fictional-model",
            "openai",
        )
        client.start_turn.return_value = "fictional-turn"
        result = TurnResult("Fictional result", 1000, 200)
        client.wait_for_turn.return_value = result
        images = (self.project.root / ".hub" / "incoming" / "image.png",)

        turn_id = start_codex_provider_turn(
            client,
            self.job(),
            thread,
            self.project,
            prompt="Fictional prompt",
            local_image_paths=images,
        )
        self.assertEqual(turn_id, "fictional-turn")
        client.start_turn.assert_called_once_with(
            thread_id="fictional-thread",
            cwd=self.project.root,
            text="Fictional prompt",
            model="fictional-model",
            effort="high",
            local_image_paths=images,
        )
        self.assertIs(wait_for_codex_provider_turn(client, turn_id), result)
        client.wait_for_turn.assert_called_once_with("fictional-turn")

    def test_external_invocation_uses_immutable_job_snapshot(self) -> None:
        adapter = Mock()
        result = ExternalTurnResult(
            "opencode",
            "Fictional result",
            "fictional-next-session",
            "fictional-actual-model",
        )
        adapter.run_turn.return_value = result
        staging = self.project.root / ".hub" / "staging" / "fictional-job"

        self.assertIs(
            invoke_external_provider_turn(
                adapter,
                self.job(),
                self.project,
                prompt="Fictional prompt",
                interrupt_prepared=True,
                staging_dir=staging,
            ),
            result,
        )
        adapter.run_turn.assert_called_once_with(
            cwd=self.project.root,
            prompt="Fictional prompt",
            session_id="fictional-provider-session",
            model="fictional-model",
            effort="high",
            interrupt_prepared=True,
            staging_dir=staging,
        )

    def test_artifact_preparation_snapshots_with_bounded_rejection_notice(self) -> None:
        artifacts = (object(),)
        with patch(
            "hermes_codex_router.worker_execution.spool_staged_artifacts",
            return_value=artifacts,
        ) as spooler:
            prepared = prepare_worker_artifacts(
                self.project.root,
                "fictional-job",
                Path("/home/example/private/hub.db"),
                report_rejections=True,
            )

        self.assertIs(prepared.artifacts, artifacts)
        self.assertEqual(prepared.visible_notice, "")
        spooler.assert_called_once()
        args = spooler.call_args
        self.assertEqual(args.args[0], self.project.root)
        self.assertEqual(args.args[1], "fictional-job")
        self.assertEqual(
            args.args[2],
            Path("/home/example/private/artifact-spool"),
        )
        self.assertEqual(args.kwargs["rejection_sink"], [])

        def reject(*_args: object, rejection_sink: list[str], **_kwargs: object) -> tuple[()]:
            rejection_sink.extend(["unsafe one", "unsafe two", "unsafe three", "unsafe four"])
            return ()

        with patch(
            "hermes_codex_router.worker_execution.spool_staged_artifacts",
            side_effect=reject,
        ):
            rejected = prepare_worker_artifacts(
                self.project.root,
                "fictional-job",
                Path("/home/example/private/hub.db"),
                report_rejections=True,
            )
        self.assertEqual(
            rejected.visible_notice,
            "\n\n⚠️ Not attached: unsafe one; unsafe two; unsafe three; and 1 more",
        )

    def test_result_preparation_preserves_runtime_specific_visible_text(self) -> None:
        incoming = PreparedIncomingMaterials("", (), ("material notice",), None, ())
        codex_result = TurnResult("  Fictional Codex result  ", 1000, 200)
        with patch(
            "hermes_codex_router.worker_execution.format_telegram_response",
            return_value="<b>codex</b>",
        ) as codex_formatter:
            codex = prepare_codex_worker_result(
                codex_result,
                incoming,
                agent_name="Codex",
                model="fictional-model",
                effort="high",
                session_label="Example Project · Fictional topic · Codex",
                limits=RateLimits(None, None),
                artifact_notice="\n\nARTIFACT NOTICE",
                trim_visible_text=True,
                empty_visible_text="Codex completed the turn without visible text.",
            )
        self.assertEqual(
            codex.visible_response,
            "Fictional Codex result\n\n⚠️ Incoming material unavailable: material notice"
            "\n\nARTIFACT NOTICE",
        )
        self.assertEqual(codex.telegram_html, "<b>codex</b>")
        formatted_result = codex_formatter.call_args.kwargs["result"]
        self.assertEqual(
            formatted_result.text,
            "  Fictional Codex result  \n\n⚠️ Incoming material unavailable: "
            "material notice\n\nARTIFACT NOTICE",
        )

        external_result = ExternalTurnResult(
            "opencode",
            "  Fictional external result  ",
            "fictional-next-session",
            "fictional-actual-model",
        )
        with patch(
            "hermes_codex_router.worker_execution.format_agent_response",
            return_value="<b>external</b>",
        ) as external_formatter:
            external = prepare_external_worker_result(
                external_result,
                incoming,
                agent_name="OpenCode",
                runtime="opencode",
                model="fictional-actual-model",
                effort="high",
                session_label="Example Project · Fictional topic · OpenCode",
                artifact_notice="\n\nARTIFACT NOTICE",
                trim_visible_text=True,
            )
        self.assertEqual(
            external.visible_response,
            "Fictional external result\n\n⚠️ Incoming material unavailable: "
            "material notice\n\nARTIFACT NOTICE",
        )
        external_formatter.assert_called_once_with(
            external.visible_response,
            {
                "Session": "Example Project · Fictional topic · OpenCode",
                "Agent": "OpenCode",
                "Runtime": "opencode",
                "Model": "fictional-actual-model",
                "Effort": "high",
                "Context remaining": "unavailable",
                "Usage windows": "unavailable",
            },
        )

    def test_failure_classification_is_conservative_after_possible_invocation(self) -> None:
        limit = ProviderLimit("opencode-go", "weekly", 0, 2_000_000_000)
        cases = (
            (
                ExecutionRootError(),
                "codex",
                ("failed", "pre_execution", "execution_root_invalid", False, "execution_root"),
            ),
            (
                ProviderTurnStopped("fictional-stop"),
                "codex",
                ("cancelled", "cancelled", "emergency_stop", False, "emergency_stop"),
            ),
            (
                ProviderLimitError(limit),
                "opencode",
                ("failed", "quota", "ProviderLimitError", False, "provider_limit"),
            ),
            (
                ProviderUnavailableError("fictional_unavailable", "Fictional unavailable"),
                "opencode",
                (
                    "failed",
                    "provider_unavailable",
                    "fictional_unavailable",
                    False,
                    "provider_unavailable",
                ),
            ),
            (
                IncomingMaterialError("fictional material"),
                "codex",
                ("failed", "pre_execution", "IncomingMaterialError", False, "incoming_material"),
            ),
            (
                CodexPreparationError("fictional setup"),
                "codex",
                ("failed", "pre_execution", "CodexPreparationError", False, "checkpoint"),
            ),
            (
                RuntimeError("completion unknown"),
                "codex",
                ("indeterminate", "ambiguous_execution", "RuntimeError", True, "checkpoint"),
            ),
            (
                RuntimeError("completion unknown"),
                "opencode",
                ("indeterminate", "ambiguous_execution", "RuntimeError", False, "uncertain"),
            ),
        )
        for error, runtime, expected in cases:
            with self.subTest(error=type(error).__name__, runtime=runtime):
                classified = classify_worker_failure(error, runtime=runtime)
                self.assertEqual(
                    (
                        classified.status,
                        classified.error_class,
                        classified.error_code,
                        classified.reconcile_codex,
                        classified.notice,
                    ),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
