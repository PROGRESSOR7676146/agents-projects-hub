from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock, patch

from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.project_resolution import ResolvedProject
from hermes_codex_router.state import ProviderJobRecord, TopicRecord
from hermes_codex_router.worker_execution import (
    WorkerExecutionTarget,
    require_provider_job_lease,
    resolve_embedded_worker_target,
    resolve_external_worker_target,
    revalidate_worker_execution_root,
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

    def job(self, lease_token: str | None = "fictional-lease") -> ProviderJobRecord:
        return cast(
            ProviderJobRecord,
            SimpleNamespace(
                job_id="fictional-job",
                topic_id=self.topic.topic_id,
                lease_token=lease_token,
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


if __name__ == "__main__":
    unittest.main()
