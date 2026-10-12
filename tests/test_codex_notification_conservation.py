"""Two owned workers conserve results while another connection receives foreign events."""

from __future__ import annotations

import copy
import json
import threading
import unittest
from collections import deque
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.codex_appserver import CodexAppServerClient, RpcError
from hermes_codex_router.codex_worker import CodexQueueWorker
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.hub_config import HubTelegramBot, ProjectBinding
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.state import HubState
from hermes_codex_router.task_activity import TaskActivityState
from tests import test_codex_worker as fixtures
from tests.git_fixtures import init_git_root
from tests.test_codex_activity_client import approval, resolved
from tests.test_codex_notification_isolation import completed, visible


def usage(thread: str, turn: str) -> dict:
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": thread,
            "turnId": turn,
            "tokenUsage": {"modelContextWindow": 1000, "last": {"totalTokens": 100}},
        },
    }


class ScriptedTransport:
    def __init__(
        self,
        *,
        name: str,
        root: Path,
        active: threading.Event,
        release: threading.Event,
        approval_count: int = 1,
    ):
        self.name, self.root, self.active, self.release = name, root, active, release
        self.thread, self.turn = f"example-thread-{name}", f"example-turn-{name}"
        self.incoming: deque[dict] = deque()
        self.sent: list[dict] = []
        self.completed_wait = False
        self.foreign_count = 0
        self.quota_used = 25 if name == "a" else 35
        self.approval_count = approval_count

    def foreign(self) -> list[dict]:
        self.foreign_count += 1200
        return [
            visible(f"Foreign {index}", thread="example-thread-a", turn="example-turn-a")
            for index in range(1200)
        ]

    def own_completion(self) -> list[dict]:
        approvals = []
        for index in range(self.approval_count):
            identifier = f"example-approval-{self.name}-{index}"
            approvals.extend(
                [
                    approval(identifier, thread=self.thread, turn=self.turn),
                    resolved(identifier, thread=self.thread),
                ]
            )
        return [
            *approvals,
            usage(self.thread, self.turn),
            {
                "method": "account/rateLimits/updated",
                "params": {
                    "rateLimits": {
                        "primary": {"usedPercent": self.quota_used, "windowDurationMins": 300}
                    },
                },
            },
            visible(f"Saved answer {self.name}", thread=self.thread, turn=self.turn),
            completed(thread=self.thread, turn=self.turn),
        ]

    def send(self, message: dict) -> None:
        self.sent.append(copy.deepcopy(message))
        method, identifier = message.get("method"), message.get("id")
        if method == "initialized":
            return
        if method in ("initialize", "thread/start", "thread/resume"):
            if method == "thread/resume":
                assert message["params"]["threadId"] == self.thread
            if self.name == "b":
                if not self.active.is_set():
                    raise AssertionError("first worker must be waiting on its accepted turn")
                self.incoming.extend(self.foreign())
            result = (
                {}
                if method == "initialize"
                else {
                    "thread": {"id": self.thread},
                    "cwd": str(self.root),
                    "approvalPolicy": "on-request",
                    "sandbox": {"type": "workspaceWrite"},
                    "modelProvider": "openai",
                }
            )
            self.incoming.append({"id": identifier, "result": result})
        elif method == "turn/start":
            assert message["params"]["threadId"] == self.thread
            if self.name == "b":
                self.incoming.extend(self.foreign())
                # A complete current turn can arrive before its RPC acknowledgement.
                if self.approval_count == 1:
                    self.incoming.extend(self.own_completion())
            self.incoming.append({"id": identifier, "result": {"turn": {"id": self.turn}}})
            if self.name == "b" and self.approval_count > 1:
                # The sequence exercises the accepted-turn pending lifecycle,
                # rather than saturating the separate preacceptance event queue.
                self.incoming.extend(self.own_completion())
        elif method == "account/rateLimits/read":
            self.incoming.append({"id": identifier, "result": {"rateLimits": {}}})
        else:
            raise AssertionError("unexpected productive or control RPC")

    def receive(self, timeout: float | None = None) -> dict:
        if not self.incoming and self.name == "a" and not self.completed_wait:
            self.active.set()
            if not self.release.wait(30):
                raise AssertionError("second worker did not reach its saved result boundary")
            self.completed_wait = True
            # The owning connection receives its own late final, never B's result.
            self.incoming.extend(self.own_completion())
        if not self.incoming:
            raise EOFError("fictional protocol script exhausted")
        return self.incoming.popleft()

    def close(self) -> None:
        pass


class InitializingSupervisor(fixtures.WorkerSupervisor):
    def client(self, **kwargs: Any) -> Any:
        cast(CodexAppServerClient, self.client_value).initialize()
        return self.client_value


class NotificationConservationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CodexQueueWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root_a = self.fixture.registry.projects[0].root.resolve()
        self.root_b = self.root_a.parent / "example-second-project"
        init_git_root(self.root_b)
        self.config = replace(
            self.fixture.config,
            max_parallel_roots=2,
            codex_worker_count=2,
            outbox_runtime="external",
            hub_bot=HubTelegramBot("example_hub_bot", self.root_a.parent / "unused-token"),
            projects=(
                *self.fixture.config.projects,
                ProjectBinding("example-second-project", -1002222222222),
            ),
        )
        self.registry = ProjectRegistry(
            1,
            self.fixture.registry.allowed_roots,
            (
                *self.fixture.registry.projects,
                Project("example-second-project", "Second", "Second", self.root_b),
            ),
        )
        document = json.loads(self.config.registry_path.read_text())
        document["projects"].append(
            {
                "project_id": "example-second-project",
                "display_name": "Second",
                "topic_name": "Second",
                "root": str(self.root_b),
            }
        )
        self.config.registry_path.write_text(json.dumps(document))

    def run_pair(self, *, resume: bool, approval_count: int = 1):
        observed = []
        original_record = TaskActivityState.record_activity

        def record_activity(activity, job_id, token, event, **kwargs):
            result = original_record(activity, job_id, token, event, **kwargs)
            observed.append((job_id, event.kind))
            return result

        activity_patch = patch.object(
            TaskActivityState, "record_activity", autospec=True, side_effect=record_activity
        )
        activity_patch.start()
        self.addCleanup(activity_patch.stop)
        first = self.fixture.enqueue()
        with closing(HubState.open(self.config.state_path, codex_permission_profile=None)) as state:
            topic = state.observe_topic(
                project_id="example-second-project",
                chat_id=-1002222222222,
                thread_id=8,
                title="Fictional second topic",
                execution_root=self.root_b,
            )
            session = state.activate_agent(topic.topic_id, "codex", "example-model", "high")
            if resume:
                session = state.bind_provider_session(session.session_id, "example-thread-b", None)
            second, _ = state.enqueue_provider_job(
                idempotency_key="example-second-worker",
                chat_id=topic.chat_id,
                message_id=2,
                topic_id=topic.topic_id,
                agent_id="codex",
                session_id=session.session_id,
                session_generation=session.generation,
                provider_session_id=session.provider_session_id,
                model=session.model,
                effort=session.effort,
                payload_text="Fictional second turn",
            )
        active, release = threading.Event(), threading.Event()
        transports = [
            ScriptedTransport(
                name=name,
                root=root,
                active=active,
                release=release,
                approval_count=approval_count,
            )
            for name, root in (("a", self.root_a), ("b", self.root_b))
        ]
        clients = [CodexAppServerClient(transport) for transport in transports]
        errors: list[BaseException] = []

        def execute_a():
            worker = CodexQueueWorker(
                self.config,
                registry=self.registry,
                supervisor=cast(Any, InitializingSupervisor(cast(Any, clients[0]))),
                worker_id="example-worker-a",
            )
            try:
                self.assertTrue(worker.run_cycle())
            except BaseException as error:
                errors.append(error)
                active.set()
            finally:
                worker.close()

        runner = threading.Thread(target=execute_a, daemon=True)
        runner.start()
        try:
            self.assertTrue(active.wait(5))
            self.assertEqual(errors, [])
            worker = CodexQueueWorker(
                self.config,
                registry=self.registry,
                supervisor=cast(Any, InitializingSupervisor(cast(Any, clients[1]))),
                worker_id="example-worker-b",
            )
            try:
                self.assertTrue(worker.run_cycle())
                current = worker.state.get_provider_job(second.job_id)
                self.assertEqual(
                    current.status, "result_ready", (current.error_code, current.error_detail)
                )
                self.assertEqual(worker.state.get_provider_job(first).status, "executing")
                accepted = ExecutionJournal(worker.state).read(first)
                assert accepted is not None
                self.assertEqual(accepted["provider_turn_id"], "example-turn-a")
            finally:
                worker.close()
        finally:
            release.set()
            runner.join(10)
        self.assertFalse(runner.is_alive())
        self.assertEqual(errors, [])
        with closing(HubState.open(self.config.state_path, codex_permission_profile=None)) as state:
            for name, job_id in (("a", first), ("b", second.job_id)):
                job = state.get_provider_job(job_id)
                self.assertEqual(job.status, "result_ready")
                checkpoint = ExecutionJournal(state).read(job_id)
                assert checkpoint is not None
                self.assertEqual(checkpoint["provider_turn_id"], f"example-turn-{name}")
                self.assertEqual(checkpoint["completed_text"], f"Saved answer {name}")
                outbox = state.get_telegram_outbox_for_job(job_id)
                self.assertIn(f"Saved answer {name}", outbox.telegram_html)
                self.assertNotIn("Foreign", outbox.telegram_html)
                self.assertIn("75%" if name == "a" else "65%", outbox.telegram_html)
                self.assertEqual(state.get_session(job.session_id).context_remaining_percent, 90)
                self.assertEqual(
                    state.get_session(job.session_id).provider_session_id, f"example-thread-{name}"
                )
                activity = state._connection.execute(
                    "SELECT kind,state FROM task_activity_entries WHERE job_id=? ORDER BY kind",
                    (job_id,),
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in activity],
                    [("approval", "resolved")] * approval_count + [("message", "completed")],
                )
                self.assertEqual(
                    [kind for observed_job, kind in observed if observed_job == job_id],
                    ["approval_requested", "approval_resolved"] * approval_count
                    + ["visible_message_completed"],
                )
        self.assertEqual(transports[1].foreign_count, 3600)
        for transport in transports:
            methods = [message.get("method") for message in transport.sent]
            self.assertEqual(methods.count("turn/start"), 1)
            self.assertTrue(all("method" in message for message in transport.sent))
            self.assertFalse(transport.incoming)
            submitted = [message for message in transport.sent if message["method"] == "turn/start"]
            self.assertEqual(submitted[0]["params"]["threadId"], transport.thread)
        preparation = [
            message
            for message in transports[1].sent
            if message["method"] in ("thread/start", "thread/resume")
        ]
        self.assertEqual(
            [message["method"] for message in preparation],
            ["thread/resume" if resume else "thread/start"],
        )
        if resume:
            self.assertEqual(preparation[0]["params"]["threadId"], "example-thread-b")

    def test_second_worker_start_conserves_both_finals_approvals_and_telemetry(self):
        self.run_pair(resume=False)

    def test_second_worker_resume_conserves_both_finals_approvals_and_telemetry(self):
        self.run_pair(resume=True)

    def test_two_workers_conserve_129_sequential_approval_pairs_and_both_results(self):
        self.run_pair(resume=True, approval_count=129)

    def test_old_unfiltered_retention_rule_reproduces_preparation_overflow(self):
        active, release = threading.Event(), threading.Event()
        active.set()
        transport = ScriptedTransport(name="b", root=self.root_b, active=active, release=release)
        client = CodexAppServerClient(transport)
        # Model the old raw-notification retention rule without widening production limits.
        with patch(
            "hermes_codex_router.codex_appserver.retain_turn_notification", return_value=True
        ):
            with self.assertRaisesRegex(RpcError, "notification buffer exceeded"):
                client.initialize()
        self.assertEqual(len(client.notifications), 1024)
        self.assertEqual([message["method"] for message in transport.sent], ["initialize"])


if __name__ == "__main__":
    unittest.main()
