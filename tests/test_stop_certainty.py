from __future__ import annotations

import threading
import unittest
from typing import Any, cast

import test_codex_worker as codex_fixtures
import test_external_worker as external_fixtures

from hermes_codex_router.codex_appserver import StoredTurnOutcome, TurnResult
from hermes_codex_router.codex_worker import CodexQueueWorker
from hermes_codex_router.root_blockers import persistent_root_blocker
from hermes_codex_router.state import HubState


class StopCertaintyTests(unittest.TestCase):
    def test_interrupt_acknowledgement_or_failure_never_proves_codex_terminal(self) -> None:
        for interrupt_fails in (False, True):
            for outcome_status in ("active", "unknown"):
                with self.subTest(interrupt_fails=interrupt_fails, outcome=outcome_status):
                    fixture = codex_fixtures.CodexQueueWorkerTests()
                    fixture.setUp()
                    job_id = fixture.enqueue()
                    entered = threading.Event()
                    released = threading.Event()
                    interrupted = threading.Event()
                    reads: list[dict[str, object]] = []

                    class MainClient(codex_fixtures.WorkerClient):
                        def wait_for_turn(self, _turn_id: str) -> TurnResult:
                            entered.set()
                            if not released.wait(3):
                                raise AssertionError("fictional stop did not reach the worker")
                            raise RuntimeError("fictional transport loss; turn outcome unconfirmed")

                    class ControlClient:
                        def interrupt_turn(self, **_kwargs: object) -> None:
                            interrupted.set()
                            released.set()
                            if interrupt_fails:
                                raise RuntimeError("fictional interrupt transport failure")

                        def close(self) -> None:
                            pass

                    class Observer:
                        def read_turn_outcome(self, **kwargs: object) -> StoredTurnOutcome:
                            reads.append(kwargs)
                            return StoredTurnOutcome(cast(Any, outcome_status))

                        def close(self) -> None:
                            pass

                    class Supervisor:
                        transport_mode = "socket"

                        def __init__(self) -> None:
                            self.main = MainClient()
                            self.calls = 0

                        def start(self) -> None:
                            pass

                        def client(self, *, allow_fallback: bool = True) -> object:
                            self.calls += 1
                            if self.calls == 1:
                                return self.main
                            return ControlClient() if self.calls == 2 else Observer()

                        def stop(self) -> None:
                            pass

                    supervisor = Supervisor()
                    worker = CodexQueueWorker(
                        fixture.config,
                        registry=fixture.registry,
                        supervisor=cast(Any, supervisor),
                        worker_id="fictional-stop-worker",
                    )
                    topic = worker.state.observe_topic(
                        project_id="example-project",
                        chat_id=-1001234567890,
                        thread_id=78,
                        title="Fictional peer",
                        execution_root=fixture.registry.projects[0].root,
                    )
                    session = worker.state.activate_agent(topic.topic_id, "codex", "model", "high")
                    peer, _ = worker.state.enqueue_provider_job(
                        idempotency_key="fictional:peer",
                        chat_id=topic.chat_id,
                        message_id=2,
                        topic_id=topic.topic_id,
                        agent_id="codex",
                        session_id=session.session_id,
                        session_generation=session.generation,
                        model=session.model,
                        effort=session.effort,
                        payload_text="Fictional peer work",
                    )

                    def request_stop() -> None:
                        if not entered.wait(2):
                            return
                        state = HubState.open(
                            fixture.config.state_path, codex_permission_profile=None
                        )
                        try:
                            active = state.get_provider_job(job_id)
                            state.request_emergency_stop(
                                topic_id=active.topic_id,
                                chat_id=active.chat_id,
                                message_id=99,
                                target_agent_id="codex",
                            )
                        finally:
                            state.close()

                    sender = threading.Thread(target=request_stop)
                    try:
                        sender.start()
                        self.assertTrue(worker.run_cycle())
                        self.assertTrue(interrupted.is_set())
                        self.assertEqual(
                            reads,
                            [
                                {
                                    "thread_id": "thread-1",
                                    "turn_id": "turn-1",
                                    "cwd": fixture.registry.projects[0].root,
                                }
                            ],
                        )
                        self.assertEqual(
                            worker.state.get_provider_job(job_id).status, "indeterminate"
                        )
                        evidence = worker.state._connection.execute(
                            "SELECT COUNT(*) FROM provider_turn_terminal_evidence WHERE job_id = ?",
                            (job_id,),
                        ).fetchone()
                        self.assertEqual(evidence[0], 0)
                        self.assertIsNotNone(
                            persistent_root_blocker(
                                worker.state._connection, topic_id=topic.topic_id
                            )
                        )
                        self.assertIsNone(
                            worker.state.lease_provider_job("codex", "fictional-peer")
                        )
                        self.assertEqual(
                            worker.state.get_provider_job(peer.job_id).status, "queued"
                        )
                        self.assertEqual(supervisor.main.turns, 1)
                    finally:
                        released.set()
                        sender.join(2)
                        worker.close()
                        fixture.tearDown()

    def test_pending_stop_does_not_hide_external_ambiguous_failure(self) -> None:
        fixture = external_fixtures.ExternalQueueWorkerTests()
        fixture.setUp()
        job_id = fixture.enqueue("opencode", 1)
        peer_id = fixture.enqueue("antigravity", 2)

        class Adapter(external_fixtures.Adapter):
            def run_turn(self, **kwargs: object) -> Any:
                self.calls += 1
                state = HubState.open(fixture.config.state_path, codex_permission_profile=None)
                try:
                    active = state.get_provider_job(job_id)
                    state.request_emergency_stop(
                        topic_id=active.topic_id,
                        chat_id=active.chat_id,
                        message_id=99,
                        target_agent_id="opencode",
                    )
                finally:
                    state.close()
                raise RuntimeError("fictional provider disconnect; owned process unconfirmed")

        adapter = Adapter("opencode")
        worker = fixture.worker("opencode", adapter)
        try:
            self.assertTrue(worker.run_cycle())
            self.assertEqual(worker.state.get_provider_job(job_id).status, "indeterminate")
            self.assertIsNone(worker.state.lease_provider_job("antigravity", "fictional-peer"))
            self.assertEqual(worker.state.get_provider_job(peer_id).status, "queued")
            self.assertEqual(adapter.calls, 1)
        finally:
            worker.close()
            fixture.tearDown()
