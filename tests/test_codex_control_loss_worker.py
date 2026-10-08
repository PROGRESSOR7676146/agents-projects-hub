"""Real Hub RPC parsing and worker state against a provider that outlives its stream."""

from __future__ import annotations

import json
import unittest
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hermes_codex_router.codex_appserver import CodexAppServerClient
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.state import HubState, StateError
from tests import test_codex_worker as fixtures
from tests.git_fixtures import init_git_root


class Provider:
    def __init__(self, root: Path, *, after_interrupt: str = "inProgress") -> None:
        self.root = root
        self.status = "inProgress"
        self.after_interrupt = after_interrupt
        self.calls: list[str] = []
        self.interrupts: list[tuple[str, str]] = []


class Transport:
    def __init__(self, provider: Provider, *, productive: bool) -> None:
        self.provider = provider
        self.productive = productive
        self.incoming: deque[dict[str, Any]] = deque()

    def send(self, message: dict[str, Any]) -> None:
        method = message["method"]
        self.provider.calls.append(method)
        if method == "initialized":
            return
        result: dict[str, Any] = {}
        if method == "thread/start":
            result = {
                "thread": {"id": "example-thread"},
                "cwd": str(self.provider.root),
                "approvalPolicy": "on-request",
                "sandbox": {"type": "workspaceWrite"},
                "modelProvider": "openai",
                "model": "gpt-5.6-sol",
            }
        elif method == "turn/start":
            assert self.productive
            result = {"turn": {"id": "example-turn"}}
        elif method == "thread/read":
            assert not self.productive
            params = message["params"]
            assert params["threadId"] == "example-thread"
            result = {
                "thread": {
                    "id": "example-thread",
                    "cwd": str(self.provider.root),
                    "turns": [
                        {
                            "id": "example-turn",
                            "status": self.provider.status,
                            "items": [
                                {
                                    "id": "example-final",
                                    "type": "agentMessage",
                                    "phase": "final_answer",
                                    "text": "Saved exact final",
                                }
                            ],
                        }
                    ]
                    if params["includeTurns"]
                    else [],
                }
            }
        elif method == "turn/interrupt":
            assert not self.productive
            params = message["params"]
            self.provider.interrupts.append((params["threadId"], params["turnId"]))
            self.provider.status = self.provider.after_interrupt
        elif method != "initialize":
            raise AssertionError("Unexpected provider control or productive replay")
        self.incoming.append({"id": message["id"], "result": result})

    def receive(self, *, timeout=None):
        if not self.incoming:
            # The native task survives this loss of its Hub activity/control stream.
            raise EOFError("Example Hub stream lost; native turn remains active")
        return self.incoming.popleft()

    def close(self):
        pass


class Supervisor(fixtures.WorkerSupervisor):
    def __init__(self, provider: Provider, main: CodexAppServerClient) -> None:
        super().__init__(main)  # type: ignore[arg-type]
        self.provider = provider
        self.control_options: list[tuple[bool, float | None]] = []

    def client(self, *, allow_fallback=True, deadline=None):
        if allow_fallback:
            return self.client_value
        self.control_options.append((allow_fallback, deadline))
        return CodexAppServerClient(Transport(self.provider, productive=False), initialized=True)


class ControlLossWorkerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CodexQueueWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def run_failure(self, after_interrupt="inProgress"):
        job_id = self.fixture.enqueue()
        provider = Provider(self.fixture.registry.projects[0].root, after_interrupt=after_interrupt)
        client = CodexAppServerClient(Transport(provider, productive=True), initialized=True)
        worker = self.fixture.worker(client)  # type: ignore[arg-type]
        supervisor = Supervisor(provider, client)
        worker.supervisor = supervisor  # type: ignore[assignment]
        self.addCleanup(worker.close)
        self.assertTrue(worker.run_cycle())
        return job_id, provider, worker, supervisor

    def test_provider_continues_after_lost_stream_and_ack_root_remains_held(self):
        job_id, provider, worker, supervisor = self.run_failure()
        self.assertEqual(provider.interrupts, [("example-thread", "example-turn")])
        self.assertEqual(provider.status, "inProgress")
        self.assertEqual(worker.state.get_provider_job(job_id).status, "indeterminate")
        checkpoint = ExecutionJournal(worker.state).read(job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["provider_turn_id"], "example-turn")
        self.assertIsNone(checkpoint["completed_text"])
        with self.assertRaises(StateError):
            self.fixture.enqueue(2, "Example owner instruction while native work continues")
        self.assertTrue(supervisor.control_options)
        self.assertTrue(
            all(
                not fallback and deadline is not None
                for fallback, deadline in supervisor.control_options
            )
        )
        self.assertEqual(provider.calls.count("turn/start"), 1)
        self.assertNotIn("thread/resume", provider.calls)
        notice = worker.state.lease_telegram_outbox("codex", "example-sender")
        assert notice is not None
        self.assertIn("root paused", notice.telegram_html)
        # Prepared notice is retained, even if Telegram delivery is unavailable.
        self.assertNotEqual(notice.status, "delivered")
        worker.run_cycle()  # Passive observation may run; productive work must not.
        self.assertEqual(provider.calls.count("turn/start"), 1)

    def test_exact_terminal_read_after_interrupt_records_proof_without_replay(self):
        job_id, provider, worker, _ = self.run_failure("interrupted")
        self.assertEqual(provider.interrupts, [("example-thread", "example-turn")])
        self.assertEqual(worker.state.get_provider_job(job_id).status, "indeterminate")
        evidence = worker.state._connection.execute(
            "SELECT * FROM provider_turn_terminal_evidence WHERE job_id=?", (job_id,)
        ).fetchone()
        self.assertIsNotNone(evidence)
        self.assertEqual(provider.calls.count("turn/start"), 1)

    def test_completed_race_recovers_exact_saved_final(self):
        job_id, provider, worker, _ = self.run_failure("completed")
        self.assertEqual(worker.state.get_provider_job(job_id).status, "result_ready")
        checkpoint = ExecutionJournal(worker.state).read(job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["completed_text"], "Saved exact final")
        self.assertEqual(provider.calls.count("turn/start"), 1)

    def test_configured_profile_drift_does_not_disable_protective_interrupt(self):
        original_guard = ExecutionJournal.can_control_accepted_turn

        def guard_with_changed_config(journal, *args, **kwargs):
            # The immutable accepted snapshot stays unchanged. New admission
            # policy must not prevent stopping the already accepted exact turn.
            journal.state._codex_permission_profile_context = "example-replacement-policy"
            return original_guard(journal, *args, **kwargs)

        with patch.object(ExecutionJournal, "can_control_accepted_turn", guard_with_changed_config):
            job_id, provider, worker, _ = self.run_failure()
        self.assertEqual(provider.interrupts, [("example-thread", "example-turn")])
        self.assertEqual(worker.state.get_provider_job(job_id).status, "indeterminate")

    def test_registry_root_drift_blocks_interrupt_even_with_legacy_project_scope(self):
        original_client = Supervisor.client
        replacement_root = self.fixture.registry.projects[0].root.parent / "example-relocated-root"
        init_git_root(replacement_root)

        def client_after_relocation(supervisor, **kwargs):
            if not kwargs.get("allow_fallback", True):
                with closing(
                    HubState.open(self.fixture.config.state_path, codex_permission_profile=None)
                ) as state:
                    state._connection.execute(
                        "UPDATE topics SET execution_scope='project:example-project'"
                    )
                    state._connection.commit()
                document = json.loads(self.fixture.config.registry_path.read_text())
                document["projects"][0]["root"] = str(replacement_root)
                self.fixture.config.registry_path.write_text(json.dumps(document))
            return original_client(supervisor, **kwargs)

        # Legacy project scope alone cannot distinguish the original root
        # from a registry relocation after native acceptance.
        with patch.object(Supervisor, "client", client_after_relocation):
            job_id, provider, worker, _ = self.run_failure()
        job = worker.state.get_provider_job(job_id)
        self.assertEqual(
            worker.state.get_topic(job.topic_id).execution_scope, "project:example-project"
        )
        self.assertEqual(provider.interrupts, [])
        self.assertEqual(job.status, "indeterminate")
        self.assertEqual(provider.calls.count("turn/start"), 1)
        self.assertIsNone(
            worker.state._connection.execute(
                "SELECT 1 FROM provider_turn_terminal_evidence WHERE job_id=?", (job_id,)
            ).fetchone()
        )
