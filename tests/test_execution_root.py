from __future__ import annotations

import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.external_service import ExternalAgentService
from hermes_codex_router.external_worker import ExternalQueueWorker
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.pilot import run_codex_pilot
from hermes_codex_router.registry import ExecutionRootError, validate_execution_root
from tests.fault_matrix_support import FaultMatrixHarness, RecordingAdapter, RecordingBot
from tests.git_fixtures import init_git_root
from tests.test_codex_worker import WorkerClient, WorkerSupervisor


class ExecutionRootTests(unittest.TestCase):
    def test_pilot_rejects_fake_git_before_state_or_supervisor_creation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            harness = FaultMatrixHarness(base / "allowed")
            root = base / "fake"
            (root / ".git").mkdir(parents=True)
            project = replace(harness.registry.projects[0], root=root)
            registry = ProjectRegistry(1, (base,), (project,))
            with (
                patch("hermes_codex_router.pilot.load_registry", return_value=registry),
                patch("hermes_codex_router.pilot.CodexAppServerSupervisor") as supervisor,
            ):
                with self.assertRaises(ExecutionRootError):
                    run_codex_pilot(
                        harness.config,
                        project_id="example-project",
                        chat_id=harness.chat_id,
                        thread_id=77,
                        topic_title="Example",
                    )
                supervisor.assert_not_called()
            self.assertFalse(harness.config.state_path.exists())

    def test_unrelated_missing_allowed_root_does_not_block_a_valid_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            init_git_root(root)
            project = Project("example-project", "Example", "Example", root)
            registry = ProjectRegistry(1, (base / "unavailable", base), (project,))
            self.assertEqual(validate_execution_root(registry, project), root)

    def test_inline_resume_and_local_terminal_transfer_refuse_changed_root(self) -> None:
        for text in ("fictional request", "/local", "/terminal"):
            with (
                self.subTest(text=text),
                tempfile.TemporaryDirectory() as directory,
                ExitStack() as stack,
            ):
                base = Path(directory)
                harness = FaultMatrixHarness(base / "allowed")
                controller = harness.controller()
                stack.callback(controller.state.close)
                controller.config = replace(harness.config, hub_bot=None, dispatch_mode="inline")
                client = WorkerClient()
                supervisor = WorkerSupervisor(client)
                controller.supervisor = cast(Any, supervisor)
                topic = controller.state.observe_topic(
                    project_id="example-project",
                    chat_id=harness.chat_id,
                    thread_id=77,
                    title="Example",
                )
                session = controller.state.activate_agent(
                    topic.topic_id, "codex", "fictional-model", "high"
                )
                controller.state.bind_provider_session(
                    session.session_id, "fictional-thread", "fictional-terminal"
                )
                root = harness.registry.projects[0].root
                root.rename(base / "outside")
                root.symlink_to(base / "outside", target_is_directory=True)
                with patch.object(controller, "_client") as create_client:
                    self.assertTrue(controller.handle_update(harness.update(101, 77, text)))
                    create_client.assert_not_called()
                current = controller.state.get_session(session.session_id)
                self.assertEqual(current.writer_mode, "telegram")
                self.assertEqual(current.provider_session_id, "fictional-thread")
                self.assertIn("locally", cast(Any, controller.telegram).sent[-1][2])
                self.assertFalse((base / "outside" / ".hub").exists())

    def test_native_direct_message_and_local_summary_refuse_changed_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            base = Path(directory)
            harness = FaultMatrixHarness(base / "allowed")
            controller = harness.controller()
            stack.callback(controller.state.close)
            service = ExternalAgentService.__new__(ExternalAgentService)
            service.config = replace(harness.config, direct_message_project_id="example-project")
            service.agent = harness.config.require_agent("opencode")
            service.registry = harness.registry
            service.state = controller.state
            service.state_path = harness.config.state_path
            service.direct_messages_only = True
            adapter = RecordingAdapter("opencode")
            service.adapter = cast(Any, adapter)
            bot = RecordingBot()
            service.telegram = cast(Any, bot)
            root = harness.registry.projects[0].root
            root.rename(base / "outside")
            root.symlink_to(base / "outside", target_is_directory=True)
            message = {
                "update_id": 101,
                "message": {
                    "message_id": 101,
                    "from": {"id": 42},
                    "chat": {"id": 42, "type": "private"},
                    "text": "fictional request",
                },
            }
            self.assertTrue(service.handle_update(message))
            self.assertFalse(service.handle_update(message))
            self.assertEqual(adapter.calls, [])
            self.assertIn("locally", bot.sent[-1][2])
            topic = service.state.find_topic(42, 1)
            assert topic is not None
            session = service.state.active_session(topic.topic_id)
            assert session is not None
            service.state.bind_provider_session(session.session_id, "fictional-thread", None)
            with self.assertRaises(ExecutionRootError):
                service.publish_local_interval(
                    chat_id=42,
                    thread_id=1,
                    topic_id=topic.topic_id,
                    project_id="example-project",
                    session_id=session.session_id,
                )
            self.assertEqual(adapter.calls, [])
            self.assertFalse((base / "outside" / ".hub").exists())

    def test_relocated_root_is_rejected_before_queue_execution(self) -> None:
        for mode in ("external", "embedded"):
            for agent in ("codex", "opencode", "antigravity"):
                with (
                    self.subTest(mode=mode, agent=agent),
                    tempfile.TemporaryDirectory() as directory,
                    ExitStack() as stack,
                ):
                    base = Path(directory)
                    harness = FaultMatrixHarness(base / "allowed")
                    root = harness.registry.projects[0].root
                    init_git_root(root)
                    controller = harness.controller()
                    stack.callback(controller.state.close)
                    adapter = RecordingAdapter(agent)
                    client = WorkerClient()
                    supervisor = WorkerSupervisor(client)
                    controller.handle_update(
                        harness.update(101, 77, f"@example_{agent}_bot fictional request")
                    )
                    topic = controller.state.find_topic(harness.chat_id, 77)
                    assert topic is not None
                    controller.state.flush_message_batch(topic.topic_id)
                    relocated = base / "outside"
                    root.rename(relocated)
                    root.symlink_to(relocated, target_is_directory=True)
                    if mode == "external":
                        worker = ExternalQueueWorker(
                            harness.config,
                            agent,
                            registry=harness.registry,
                            adapter=cast(Any, adapter),
                            supervisor=cast(Any, supervisor),
                        )
                        try:
                            self.assertTrue(worker.run_cycle())
                            self.assertFalse(worker.run_cycle())
                        finally:
                            worker.close()
                    else:
                        controller.config = replace(
                            harness.config,
                            hub_bot=None,
                            queue_runtime="embedded",
                            external_worker_agent_ids=(),
                        )
                        controller.supervisor = cast(Any, supervisor)
                        controller.external_services = cast(
                            Any, {agent: type("External", (), {"adapter": adapter})()}
                        )
                        job = controller.state.lease_provider_job(agent, "fictional-worker")
                        assert job is not None
                        controller._execute_embedded_provider_job(controller.state, job)
                    self.assertEqual(client.turns, 0)
                    self.assertEqual(adapter.calls, [])
                    self.assertFalse(supervisor.started)
                    self.assertFalse((relocated / ".hub").exists())
                    job = controller.state.provider_jobs_for_topic(topic.topic_id)[0]
                    self.assertEqual(job.status, "failed")
                    self.assertEqual(job.error_class, "pre_execution")
                    self.assertEqual(job.error_code, "execution_root_invalid")
                    notice = controller.state.lease_telegram_outbox(agent, "fictional-sender")
                    assert notice is not None
                    self.assertIn("local", notice.telegram_html)
                    self.assertNotIn(str(base), notice.telegram_html)

    def test_invalid_filesystem_shapes_never_pass_as_the_intended_git_root(self) -> None:
        for shape in (
            "missing",
            "file",
            "fake_git",
            "nested",
            "symlink_inside",
            "symlink_outside",
            "allowlist_replaced",
        ):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                allowed = base / "allowed"
                root = allowed / "project"
                init_git_root(root)
                project = Project("example-project", "Example", "Example", root)
                registry = ProjectRegistry(1, (allowed,), (project,))
                if shape == "allowlist_replaced":
                    allowed.rename(base / "moved")
                    allowed.symlink_to(base / "moved", target_is_directory=True)
                else:
                    root.rename(allowed / "original")
                    if shape == "file":
                        root.write_text("fictional file", encoding="utf-8")
                    elif shape == "fake_git":
                        (root / ".git").mkdir(parents=True)
                    elif shape == "nested":
                        init_git_root(allowed)
                        root.mkdir()
                    elif shape.startswith("symlink"):
                        target = (
                            allowed / "original" if shape == "symlink_inside" else base / "outside"
                        )
                        init_git_root(target)
                        root.symlink_to(target, target_is_directory=True)
                with self.assertRaises(ExecutionRootError) as error:
                    validate_execution_root(registry, project)
                self.assertNotIn(str(base), str(error.exception))

    def test_real_git_root_and_linked_worktree_are_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            init_git_root(root)
            subprocess.run(
                (
                    "git",
                    "-C",
                    str(root),
                    "-c",
                    "user.name=Example",
                    "-c",
                    "user.email=example@example.com",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    "core.hooksPath=/dev/null",
                    "commit",
                    "--allow-empty",
                    "-qm",
                    "Fictional fixture",
                ),
                check=True,
                capture_output=True,
                timeout=5,
            )
            lane = base / "lane"
            subprocess.run(
                ("git", "-C", str(root), "worktree", "add", "--detach", str(lane)),
                check=True,
                capture_output=True,
                timeout=5,
            )
            self.assertTrue((lane / ".git").is_file())
            for candidate in (root, lane):
                project = Project("example-project", "Example", "Example", candidate)
                registry = ProjectRegistry(1, (base,), (project,))
                self.assertEqual(validate_execution_root(registry, project), candidate)
                # A service's inherited Git environment must not redirect this check.
                with patch.dict(
                    "os.environ", {"GIT_DIR": str(base / "absent"), "GIT_WORK_TREE": str(base)}
                ):
                    self.assertEqual(validate_execution_root(registry, project), candidate)

    def test_git_timeout_or_failure_has_only_a_bounded_public_refusal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_git_root(root)
            project = Project("example-project", "Example", "Example", root)
            registry = ProjectRegistry(1, (root,), (project,))
            for error in (OSError("private details"), subprocess.TimeoutExpired("git", 5)):
                with patch("hermes_codex_router.registry.subprocess.run", side_effect=error):
                    with self.assertRaises(ExecutionRootError) as caught:
                        validate_execution_root(registry, project)
                    self.assertNotIn("private details", str(caught.exception))
