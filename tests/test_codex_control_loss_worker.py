"""Real Hub RPC parsing and worker state against a provider that outlives its stream."""

from __future__ import annotations

import json
import unittest
from collections import deque
from contextlib import closing
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.codex_appserver import CodexAppServerClient
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.state import HubState, StateError
from tests import test_codex_worker as fixtures
from tests import test_embedded_queue_service as embedded
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
    def __init__(self, provider: Provider, main: CodexAppServerClient, *, stdio=False) -> None:
        super().__init__(main)  # type: ignore[arg-type]
        self.provider = provider
        self.control_options: list[tuple[bool, float | None]] = []
        self.stdio = stdio
        self.transport_mode = "stdio-fallback" if stdio else "socket"
        self.acquisitions = 0
        self.mode_after_acquisition = None

    def client(self, *, allow_fallback=True, deadline=None) -> Any:
        self.acquisitions += 1
        if self.acquisitions == 1:
            if self.mode_after_acquisition is not None:
                self.transport_mode = self.mode_after_acquisition
            return self.client_value
        self.control_options.append((allow_fallback, deadline))
        if (self.stdio or self.transport_mode == "stdio-fallback") and not allow_fallback:
            raise RuntimeError("No owning shared socket in example stdio mode")
        return CodexAppServerClient(Transport(self.provider, productive=False), initialized=True)


class ControlLossWorkerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CodexQueueWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def run_failure(
        self,
        after_interrupt="inProgress",
        *,
        stdio=False,
        failure_mode=None,
        acquisition_mode=None,
        unbound=False,
    ):
        job_id = self.fixture.enqueue()
        provider = Provider(self.fixture.registry.projects[0].root, after_interrupt=after_interrupt)
        if stdio:
            provider.status = after_interrupt
        client = CodexAppServerClient(
            Transport(provider, productive=True),
            initialized=True,
            transport_mode=None if unbound else "stdio-fallback" if stdio else "socket",
        )
        worker = self.fixture.worker(client)  # type: ignore[arg-type]
        supervisor = Supervisor(provider, client, stdio=stdio)
        supervisor.mode_after_acquisition = acquisition_mode
        worker.supervisor = supervisor  # type: ignore[assignment]
        self.addCleanup(worker.close)
        original_wait = client.wait_for_turn

        def wait_with_other_client_mode(*args, **kwargs):
            if failure_mode is not None:
                supervisor.transport_mode = failure_mode
            return original_wait(*args, **kwargs)

        with patch.object(client, "wait_for_turn", side_effect=wait_with_other_client_mode):
            self.assertTrue(worker.run_cycle())
        return job_id, provider, worker, supervisor

    def test_mode_change_before_acquisition_returns_cannot_retarget_socket_turn(self):
        job_id, provider, worker, supervisor = self.run_failure(acquisition_mode="stdio-fallback")
        self.assertEqual(worker.state.get_provider_job(job_id).status, "indeterminate")
        self.assertEqual(provider.interrupts, [])
        self.assertFalse(supervisor.control_options[0][0])

    def test_unknown_primary_transport_grants_no_protective_control_or_fallback(self):
        job_id, provider, worker, supervisor = self.run_failure(unbound=True)
        self.assertEqual(worker.state.get_provider_job(job_id).status, "indeterminate")
        self.assertEqual(provider.interrupts, [])
        self.assertFalse(supervisor.control_options[0][0])

    def test_other_client_fallback_cannot_change_socket_turn_recovery(self):
        job_id, provider, worker, supervisor = self.run_failure(failure_mode="stdio-fallback")
        self.assertEqual(worker.state.get_provider_job(job_id).status, "indeterminate")
        self.assertEqual(provider.interrupts, [])
        self.assertFalse(supervisor.control_options[0][0])

    def test_other_client_socket_restore_cannot_grant_stdio_turn_control(self):
        job_id, provider, worker, supervisor = self.run_failure(
            "completed", stdio=True, failure_mode="socket"
        )
        self.assertEqual(worker.state.get_provider_job(job_id).status, "result_ready")
        self.assertEqual(provider.interrupts, [])
        self.assertTrue(supervisor.control_options[0][0])

    def test_protective_send_and_ack_are_durable_and_notice_identifies_hub_stop(self):
        job_id, provider, worker, _ = self.run_failure()
        events = worker.state._connection.execute(
            "SELECT code, detail FROM runtime_events WHERE code LIKE 'codex_protective_interrupt%'"
        ).fetchall()
        self.assertEqual(
            [row["code"] for row in events],
            ["codex_protective_interrupt_attempted", "codex_protective_interrupt_acknowledged"],
        )
        self.assertTrue(all(row["detail"] == job_id for row in events))
        notice = worker.state.lease_telegram_outbox("codex", "example-sender")
        assert notice is not None
        self.assertIn("Hub attempted to interrupt this exact turn", notice.telegram_html)
        self.assertEqual(provider.status, "inProgress")

    def test_stdio_completed_final_recovers_read_only_without_owning_socket(self):
        job_id, provider, worker, supervisor = self.run_failure("completed", stdio=True)
        self.assertEqual(worker.state.get_provider_job(job_id).status, "result_ready")
        self.assertEqual(provider.interrupts, [])
        self.assertEqual(provider.calls.count("turn/start"), 1)
        self.assertNotIn("thread/resume", provider.calls)
        self.assertTrue(supervisor.control_options[0][0])

    def test_stdio_active_keeps_root_and_never_interrupts_a_nonowning_process(self):
        job_id, provider, worker, _ = self.run_failure("inProgress", stdio=True)
        self.assertEqual(worker.state.get_provider_job(job_id).status, "indeterminate")
        self.assertEqual(provider.interrupts, [])
        with self.assertRaises(StateError):
            self.fixture.enqueue(2, "Example held root")

    def test_stdio_unknown_does_not_replay_or_interrupt(self):
        job_id, provider, worker, _ = self.run_failure("unknown", stdio=True)
        self.assertEqual(worker.state.get_provider_job(job_id).status, "indeterminate")
        self.assertEqual(provider.interrupts, [])
        self.assertEqual(provider.calls.count("turn/start"), 1)

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
        notice = worker.state.lease_telegram_outbox("codex", "example-sender")
        assert notice is not None
        self.assertIn("Hub attempted to interrupt this exact turn", notice.telegram_html)

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


class EmbeddedStdioRecoveryTests(unittest.TestCase):
    def run_failure(self, status, *, stdio=True, failure_mode=None, acquisition_mode=None):
        fixture = embedded.EmbeddedQueueServiceTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        provider = Provider(fixture.registry.projects[0].root)
        provider.status = status
        main = CodexAppServerClient(
            Transport(provider, productive=True),
            initialized=True,
            transport_mode="stdio-fallback" if stdio else "socket",
        )
        service, _ = fixture.service(main)  # type: ignore[arg-type]
        supervisor = Supervisor(provider, main, stdio=stdio)
        supervisor.mode_after_acquisition = acquisition_mode
        service.supervisor = supervisor  # type: ignore[assignment]
        self.addCleanup(service.close)
        self.assertTrue(service.handle_update(embedded.update(1, "Example retained task")))
        original_wait = main.wait_for_turn

        def wait_with_other_client_mode(*args, **kwargs):
            if failure_mode is not None:
                supervisor.transport_mode = failure_mode
            return original_wait(*args, **kwargs)

        with patch.object(main, "wait_for_turn", side_effect=wait_with_other_client_mode):
            self.assertTrue(service.run_embedded_queue_cycle())
        topic = service.state.find_topic(-1001234567890, 77)
        assert topic is not None
        job = service.state.provider_jobs_for_topic(topic.topic_id)[0]
        return job, provider, service

    def test_mode_change_before_acquisition_returns_cannot_retarget_socket_turn(self):
        job, provider, service = self.run_failure(
            "inProgress", stdio=False, acquisition_mode="stdio-fallback"
        )
        self.assertEqual(job.status, "indeterminate")
        self.assertEqual(provider.interrupts, [])
        self.assertFalse(cast(Supervisor, service.supervisor).control_options[0][0])

    def test_other_client_fallback_cannot_change_socket_turn_recovery(self):
        job, provider, service = self.run_failure(
            "inProgress", stdio=False, failure_mode="stdio-fallback"
        )
        self.assertEqual(job.status, "indeterminate")
        self.assertEqual(provider.interrupts, [])
        self.assertFalse(cast(Supervisor, service.supervisor).control_options[0][0])

    def test_other_client_socket_restore_cannot_grant_stdio_turn_control(self):
        job, provider, _ = self.run_failure("completed", failure_mode="socket")
        self.assertEqual(job.status, "completed")
        self.assertEqual(provider.interrupts, [])

    def test_stdio_saved_completion_survives_primary_loss_without_interrupt(self):
        job, provider, service = self.run_failure("completed")
        self.assertEqual(job.status, "completed")
        checkpoint = ExecutionJournal(service.state).read(job.job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["completed_text"], "Saved exact final")
        self.assertEqual(provider.interrupts, [])
        self.assertEqual(provider.calls.count("turn/start"), 1)
        self.assertNotIn("thread/resume", provider.calls)

    def test_stdio_active_keeps_uncertainty_without_interrupt(self):
        job, provider, _ = self.run_failure("inProgress")
        self.assertEqual(job.status, "indeterminate")
        self.assertEqual(provider.interrupts, [])
        self.assertEqual(provider.calls.count("turn/start"), 1)

    def test_stdio_unknown_keeps_uncertainty_without_replay(self):
        job, provider, _ = self.run_failure("unknown")
        self.assertEqual(job.status, "indeterminate")
        self.assertEqual(provider.interrupts, [])
        self.assertEqual(provider.calls.count("turn/start"), 1)
