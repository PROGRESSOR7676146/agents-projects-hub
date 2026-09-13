from __future__ import annotations

import subprocess
import unittest
from dataclasses import replace
from typing import Any, cast
from unittest.mock import patch

import test_codex_worker as fixtures

from hermes_codex_router.codex_appserver import CodexThread, CodexThreadMetadata, RpcError
from hermes_codex_router.codex_worker import CodexQueueWorker
from hermes_codex_router.session_adoption_policy import validate_adoption_mode
from hermes_codex_router.session_adoption_state import AdoptionRequest, CodexSessionOrigins
from hermes_codex_router.state import HubState


class SessionAdoptionWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexQueueWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.config = replace(self.fixture.config, outbox_runtime="external")
        self.root = self.fixture.registry.projects[0].root
        subprocess.run(("git", "init", "-q", str(self.root)), check=True)
        self.state = HubState.open(self.config.state_path)
        self.addCleanup(self.state.close)
        self.topic = self.state.observe_topic(
            project_id="example-project", chat_id=-1001234567890, thread_id=7, title="Example"
        )
        self.origins = CodexSessionOrigins(self.state)
        self.session = self.origins.attach(
            AdoptionRequest(
                "example-project",
                self.topic.chat_id,
                7,
                "saved-cli-thread",
                self.root,
                "gpt-5.6-sol",
                "high",
            ),
            expected_session_id=None,
        ).session
        self.state.return_codex_local_writer(
            chat_id=self.topic.chat_id,
            message_id=10,
            topic_id=self.topic.topic_id,
            session_id=self.session.session_id,
            observer_agent_id="hub",
        )

    def enqueue(self, message_id=11):
        return self.state.enqueue_provider_job(
            idempotency_key=f"example:{message_id}",
            chat_id=self.topic.chat_id,
            message_id=message_id,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            provider_session_id=self.session.provider_session_id,
            model=self.session.model,
            effort=self.session.effort,
            payload_text="Continue existing conversation",
        )[0]

    def client(self, *, failure=None):
        root = self.root

        class Client(fixtures.WorkerClient):
            def __init__(self):
                super().__init__()
                self.calls = []

            def read_thread_metadata(self, **kwargs):
                self.calls.append(("read", kwargs["thread_id"]))
                if failure == "read":
                    raise RpcError("private read failure")
                return CodexThreadMetadata("saved-cli-thread", root, "openai", "notLoaded")

            def start_thread(self, **kwargs):
                self.calls.append(("start", None))
                return super().start_thread(**kwargs)

            def resume_thread(self, **kwargs):
                self.calls.append(("resume", kwargs["thread_id"]))
                if failure == "resume":
                    raise RpcError("private writer lock")
                if failure == "wrong-thread":
                    return CodexThread("unexpected-thread", root, "gpt-5.6-sol", "openai")
                if failure == "wrong-root":
                    return CodexThread("saved-cli-thread", root.parent, "gpt-5.6-sol", "openai")
                return CodexThread("saved-cli-thread", root, "gpt-5.6-sol", "openai")

            def start_turn(self, **kwargs):
                self.calls.append(("turn", kwargs["thread_id"]))
                return super().start_turn(**kwargs)

        return Client()

    def run_worker(self, client, mode="stdio-fallback"):
        supervisor = fixtures.WorkerSupervisor(client)
        cast(Any, supervisor).transport_mode = mode
        worker = CodexQueueWorker(
            self.config, registry=self.fixture.registry, supervisor=cast(Any, supervisor)
        )
        try:
            self.assertTrue(worker.run_cycle())
        finally:
            worker.close()

    def test_stdio_and_socket_resume_exact_thread_without_fallback_transfer(self) -> None:
        for index, mode in enumerate(("stdio-fallback", "socket")):
            with self.subTest(mode=mode):
                job = self.enqueue(11 + index)
                client = self.client()
                self.run_worker(client, mode)
                self.assertEqual(
                    client.calls,
                    [
                        ("read", "saved-cli-thread"),
                        ("resume", "saved-cli-thread"),
                        ("turn", "saved-cli-thread"),
                    ],
                )
                self.assertEqual(
                    self.state.get_session(self.session.session_id).provider_session_id,
                    "saved-cli-thread",
                )
                self.assertEqual(self.state.get_provider_job(job.job_id).status, "result_ready")
                # Simulate successful transport acceptance to release topic FIFO.
                with self.state._connection:
                    self.state._connection.execute(
                        "UPDATE provider_jobs SET status='completed' WHERE job_id=?", (job.job_id,)
                    )

    def test_metadata_or_resume_failure_never_starts_productive_or_replacement_thread(self) -> None:
        for index, failure in enumerate(("read", "resume", "wrong-thread", "wrong-root")):
            with self.subTest(failure=failure):
                job = self.enqueue(20 + index)
                client = self.client(failure=failure)
                self.run_worker(client)
                self.assertFalse(any(name in ("start", "turn") for name, _ in client.calls))
                self.assertEqual(self.state.get_provider_job(job.job_id).status, "failed")
                self.assertEqual(
                    self.origins.require(self.session.session_id).provider_thread_id,
                    "saved-cli-thread",
                )

    def test_origin_binding_cannot_change_or_disappear(self) -> None:
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.state.bind_provider_session(self.session.session_id, "replacement-thread", None)
        with self.assertRaises(sqlite3.IntegrityError), self.state._connection:
            self.state._connection.execute("DELETE FROM codex_session_origins")

    def test_post_acceptance_failure_is_indeterminate_and_not_replayed(self) -> None:
        job = self.enqueue()
        client = self.client()
        client.fail_after_start = True
        self.run_worker(client)
        self.assertEqual(self.state.get_provider_job(job.job_id).status, "indeterminate")
        later = self.client()
        supervisor = fixtures.WorkerSupervisor(later)
        cast(Any, supervisor).transport_mode = "stdio-fallback"
        worker = CodexQueueWorker(
            self.config, registry=self.fixture.registry, supervisor=cast(Any, supervisor)
        )
        try:
            worker.run_cycle()
        finally:
            worker.close()
        self.assertFalse(any(name in ("start", "turn") for name, _ in later.calls))
        self.assertEqual(
            self.origins.require(self.session.session_id).provider_thread_id, "saved-cli-thread"
        )

    def test_mode_rollback_rejects_retained_origins(self) -> None:
        validate_adoption_mode(self.config)
        for config in (
            replace(self.config, dispatch_mode="inline"),
            replace(self.config, queue_runtime="embedded"),
            replace(self.config, outbox_runtime="controller"),
        ):
            with self.assertRaises(ValueError):
                validate_adoption_mode(config)
        self.state.new_active_session(self.topic.topic_id)
        with self.assertRaises(ValueError):
            validate_adoption_mode(replace(self.config, dispatch_mode="inline"))

    def test_pilot_cannot_bypass_adopted_execution_policy(self) -> None:
        from hermes_codex_router.pilot import run_codex_pilot

        with patch("hermes_codex_router.pilot.CodexAppServerSupervisor") as supervisor:
            with self.assertRaisesRegex(ValueError, "adopted"):
                run_codex_pilot(
                    self.config,
                    project_id="example-project",
                    chat_id=self.topic.chat_id,
                    thread_id=7,
                    topic_title="Example",
                )
            supervisor.assert_not_called()
