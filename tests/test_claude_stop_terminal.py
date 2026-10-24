"""A proven local Claude exit may settle only its covering owner stop."""

from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from hermes_codex_router import external_runtime
from hermes_codex_router.external_runtime import ExternalCliAdapter, ExternalTurnInterrupted
from hermes_codex_router.root_blockers import persistent_root_blocker
from hermes_codex_router.state_errors import StateError
from tests import test_claude_native_worker as native_fixtures
from tests.test_external_runtime import fictional_claude_source

SESSION = "00000000-0000-4000-8000-000000000001"
OTHER = "019abcde-1234-7fff-8fff-0123456789ab"


class ClaudeStopTerminalTests(unittest.TestCase):
    def test_owned_runner_carries_session_proof_only_after_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "provider"
            event = {
                "type": "assistant",
                "uuid": OTHER,
                "session_id": SESSION,
                "parent_tool_use_id": None,
                "message": {"content": [{"type": "text", "text": "Fictional partial"}]},
            }
            child.write_text(
                fictional_claude_source(
                    "import os,time\n"
                    f"os.write(1, {(json.dumps(event) + chr(10)).encode()!r})\n"
                    "time.sleep(30)\n"
                )
            )
            child.chmod(0o700)
            adapter = ExternalCliAdapter("claude", executable=str(child))
            processes: list[subprocess.Popen[str]] = []

            def stop(_item: object) -> None:
                assert adapter._active_process is not None
                processes.append(adapter._active_process)
                self.assertTrue(adapter.interrupt())

            with (
                patch.dict(
                    "os.environ",
                    {
                        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                        "ANTHROPIC_AUTH_TOKEN": "example",
                    },
                    clear=True,
                ),
                self.assertRaises(ExternalTurnInterrupted) as interrupted,
            ):
                adapter.run_turn(
                    cwd=root,
                    prompt="Fictional work",
                    new_session_id=SESSION,
                    timeout=5,
                    on_visible_assistant=stop,
                )
            self.assertEqual(getattr(interrupted.exception, "session_id", None), SESSION)
            self.assertEqual(type(interrupted.exception).__name__, "ClaudeProcessStopped")
            self.assertIsNone(adapter._active_process)
            self.assertIsNotNone(processes[0].returncode)
            self.assertTrue(processes[0].stdout and processes[0].stdout.closed)
            self.assertTrue(processes[0].stderr and processes[0].stderr.closed)

    def test_covering_stop_requires_exact_session_and_local_exit_proof(self) -> None:
        for proof, stop, wrong_session in (
            (True, True, False),
            (False, True, False),
            (True, False, False),
            (True, True, True),
        ):
            with self.subTest(proof=proof, stop=stop, wrong_session=wrong_session):
                fixture = native_fixtures.ClaudeNativeWorkerTests()
                fixture.setUp()
                try:
                    initial_id = fixture.enqueue(1)
                    initial_adapter = native_fixtures.ObservingClaudeAdapter(fixture.path)
                    initial_worker = fixture.worker(initial_adapter)
                    self.assertTrue(initial_worker.run_cycle())
                    native = initial_worker.state.get_provider_result(
                        initial_id
                    ).provider_session_id
                    # Fixture delivery acknowledges the established native conversation.
                    with initial_worker.state._connection:
                        initial_worker.state._connection.execute(
                            "UPDATE provider_jobs SET status='completed' WHERE job_id=?",
                            (initial_id,),
                        )
                        initial_worker.state._connection.execute(
                            "UPDATE telegram_outbox SET status='delivered' WHERE job_id=?",
                            (initial_id,),
                        )
                    job_id = fixture.enqueue(2)

                    class Adapter(native_fixtures.ObservingClaudeAdapter):
                        def run_turn(self, **kwargs: Any) -> Any:
                            result = super().run_turn(**kwargs)
                            if proof:
                                kind = getattr(
                                    external_runtime,
                                    "ClaudeProcessStopped",
                                    ExternalTurnInterrupted,
                                )
                                raise kind(OTHER if wrong_session else result.provider_session_id)
                            raise ExternalTurnInterrupted("Native outcome is unconfirmed")

                    adapter = Adapter(fixture.path, stop=stop)
                    worker = fixture.worker(adapter)
                    self.assertTrue(worker.run_cycle())
                    job = worker.state.get_provider_job(job_id)
                    stop_row = worker.state._connection.execute(
                        "SELECT status FROM provider_stop_requests WHERE topic_id=?",
                        (job.topic_id,),
                    ).fetchone()
                    fixture.assert_no_result(worker.state, job_id)
                    if proof and stop and not wrong_session:
                        self.assertEqual(job.status, "cancelled")
                        self.assertEqual(stop_row[0], "completed")
                        self.assertIsNone(
                            persistent_root_blocker(worker.state._connection, topic_id=job.topic_id)
                        )
                        # Later work keeps the native session and can acquire the root.
                        later = fixture.enqueue(3)
                        resumed_adapter = native_fixtures.ObservingClaudeAdapter(fixture.path)
                        resumed_worker = fixture.worker(resumed_adapter)
                        self.assertTrue(resumed_worker.run_cycle())
                        self.assertEqual(resumed_adapter.calls[0]["session_id"], native)
                        self.assertIsNone(resumed_adapter.calls[0]["new_session_id"])
                        self.assertEqual(
                            resumed_worker.state.get_provider_job(later).status, "result_ready"
                        )
                    else:
                        self.assertEqual(job.status, "indeterminate")
                        if stop:
                            self.assertEqual(stop_row[0], "pending")
                        self.assertIsNotNone(
                            persistent_root_blocker(worker.state._connection, topic_id=job.topic_id)
                        )
                    self.assertFalse(worker.run_cycle())
                    self.assertEqual(len(adapter.calls), 1)
                finally:
                    fixture.doCleanups()

    def test_stop_on_first_native_attempt_cannot_create_an_unresumable_binding(self) -> None:
        fixture = native_fixtures.ClaudeNativeWorkerTests()
        fixture.setUp()
        try:
            first = fixture.enqueue(1)

            class Adapter(native_fixtures.ObservingClaudeAdapter):
                def run_turn(self, **kwargs: Any) -> Any:
                    result = super().run_turn(**kwargs)
                    raise external_runtime.ClaudeProcessStopped(result.provider_session_id)

            adapter = Adapter(fixture.path, stop=True)
            worker = fixture.worker(adapter)
            self.assertTrue(worker.run_cycle())
            self.assertIsNone(adapter.calls[0]["session_id"])
            self.assertIsNotNone(adapter.calls[0]["new_session_id"])
            fixture.assert_uncertain_and_not_replayed(worker, adapter, first)
            job = worker.state.get_provider_job(first)
            with self.assertRaisesRegex(StateError, "uncertainty"):
                worker.state.enqueue_provider_job(
                    idempotency_key="example-after-incomplete-session",
                    chat_id=job.chat_id,
                    message_id=2,
                    topic_id=job.topic_id,
                    agent_id=job.agent_id,
                    session_id=job.session_id,
                    session_generation=job.session_generation,
                    provider_session_id=worker.state.get_session(
                        job.session_id
                    ).provider_session_id,
                    model=job.model,
                    effort=job.effort,
                    payload_text="Fictional later request",
                )
            self.assertFalse(worker.run_cycle())
            self.assertEqual(len(adapter.calls), 1)
            self.assertEqual(
                worker.state._connection.execute(
                    "SELECT status FROM provider_stop_requests WHERE topic_id=?",
                    (worker.state.get_provider_job(first).topic_id,),
                ).fetchone()[0],
                "pending",
            )
        finally:
            fixture.doCleanups()

    def test_failed_owned_cleanup_cannot_be_promoted_to_stop_proof(self) -> None:
        adapter = ExternalCliAdapter("claude")
        adapter.prepare_interruptible_turn()
        sandbox = MagicMock()
        sandbox.claude_executable = Path("/example/provider")
        launch = MagicMock()
        launch.__exit__.side_effect = RuntimeError("Mount cleanup unconfirmed")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(adapter, "_verified_claude_argv", return_value=("/example/provider",)),
            patch("hermes_codex_router.external_runtime.wrap_file_tool_argv", return_value=launch),
            patch.object(
                adapter, "_run_claude_process", side_effect=ExternalTurnInterrupted("Owner stop")
            ),
            self.assertRaisesRegex(RuntimeError, "Mount cleanup unconfirmed"),
        ):
            adapter._run_owned_claude_turn(
                ("/example/provider",),
                cwd=Path(directory),
                environment={},
                timeout=5,
                expected_session_id=SESSION,
                on_visible_assistant=None,
                on_process_started=None,
                sandbox=sandbox,
            )
        launch.__exit__.assert_called_once()

    def test_failed_process_cleanup_remains_uncertain_after_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "provider"
            event = {
                "type": "assistant",
                "uuid": OTHER,
                "session_id": SESSION,
                "parent_tool_use_id": None,
                "message": {"content": [{"type": "text", "text": "Fictional partial"}]},
            }
            child.write_text(
                fictional_claude_source(
                    "import os,time\n"
                    f"os.write(1, {(json.dumps(event) + chr(10)).encode()!r})\n"
                    "time.sleep(30)\n"
                )
            )
            child.chmod(0o700)
            adapter = ExternalCliAdapter("claude", executable=str(child))
            original_cleanup = adapter._terminate_claude_process

            def failed_cleanup(process: subprocess.Popen[str], *, graceful: bool) -> None:
                original_cleanup(process, graceful=graceful)
                raise RuntimeError("Process cleanup unconfirmed")

            def stop(_item: object) -> None:
                adapter.interrupt()

            with (
                patch.dict(
                    "os.environ",
                    {
                        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                        "ANTHROPIC_AUTH_TOKEN": "example",
                    },
                    clear=True,
                ),
                patch.object(adapter, "_terminate_claude_process", side_effect=failed_cleanup),
                self.assertRaisesRegex(RuntimeError, "Process cleanup unconfirmed"),
            ):
                adapter.run_turn(
                    cwd=root,
                    prompt="Fictional work",
                    new_session_id=SESSION,
                    timeout=5,
                    on_visible_assistant=stop,
                )
            self.assertIsNone(adapter._active_process)


if __name__ == "__main__":
    unittest.main()
