"""Real owned CLI/worker stop races must preserve contradictory evidence."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router import claude_cli_capabilities
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.external_runtime import ExternalCliAdapter
from hermes_codex_router.root_blockers import persistent_root_blocker
from tests import test_claude_native_worker as fixtures
from tests.test_external_runtime import fictional_claude_source


class ClaudeStopPrecedenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(
            patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                    "ANTHROPIC_AUTH_TOKEN": "example",
                },
                clear=True,
            )
        )

    def fixture(self, *, established: bool = True) -> tuple[Any, str]:
        fixture = fixtures.ClaudeNativeWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if established:
            first = fixture.enqueue(1)
            initial = fixture.worker(fixtures.ObservingClaudeAdapter(fixture.path))
            self.assertTrue(initial.run_cycle())
            with initial.state._connection:
                initial.state._connection.execute(
                    "UPDATE provider_jobs SET status='completed' WHERE job_id=?", (first,)
                )
                initial.state._connection.execute(
                    "UPDATE telegram_outbox SET status='delivered' WHERE job_id=?", (first,)
                )
        return fixture, fixture.enqueue(2 if established else 1)

    def adapter(self, root: Path, body: str) -> ExternalCliAdapter:
        child = root / "fictional-provider"
        child.write_text(
            fictional_claude_source(
                "import json,os,sys,time\n"
                "native=next(sys.argv[sys.argv.index(flag)+1] for flag in "
                "('--session-id','--resume') if flag in sys.argv)\n" + body
            )
        )
        child.chmod(0o700)
        return ExternalCliAdapter("claude", executable=str(child))

    def request_stop(self, worker: Any, job_id: str, adapter: ExternalCliAdapter) -> None:
        job = worker.state.get_provider_job(job_id)
        worker.state.request_emergency_stop(
            topic_id=job.topic_id, chat_id=job.chat_id, message_id=99, target_agent_id="claude"
        )
        adapter.interrupt()

    def assert_state(self, worker: Any, job_id: str, *, uncertain: bool) -> None:
        job = worker.state.get_provider_job(job_id)
        self.assertEqual(job.status, "indeterminate" if uncertain else "cancelled", job.error_code)
        stop = worker.state._connection.execute(
            "SELECT status FROM provider_stop_requests WHERE topic_id=?", (job.topic_id,)
        ).fetchone()
        self.assertEqual(stop[0], "pending" if uncertain else "completed")
        blocker = persistent_root_blocker(worker.state._connection, topic_id=job.topic_id)
        self.assertEqual(blocker is not None, uncertain)
        self.assertIsNone(
            worker.state._connection.execute(
                "SELECT result_id FROM provider_job_results WHERE job_id=?", (job_id,)
            ).fetchone()
        )
        self.assertFalse(worker.run_cycle())

    def test_stop_before_capability_check_requires_established_session(self) -> None:
        for established in (False, True):
            with self.subTest(established=established):
                fixture, job_id = self.fixture(established=established)
                adapter = self.adapter(fixture.root, "raise AssertionError('must not launch')\n")
                worker = fixture.worker(cast(Any, adapter))
                verify = adapter._verified_claude_argv

                def stopped(*args: Any, **kwargs: Any) -> Any:
                    self.request_stop(worker, job_id, adapter)
                    return verify(*args, **kwargs)

                with (
                    patch.object(adapter, "_verified_claude_argv", side_effect=stopped),
                    patch.object(adapter, "_run_claude_process") as run,
                ):
                    self.assertTrue(worker.run_cycle())
                run.assert_not_called()
                self.assert_state(worker, job_id, uncertain=not established)

    def test_help_cleanup_failure_is_not_promoted_to_stop_proof(self) -> None:
        fixture, job_id = self.fixture()
        adapter = self.adapter(fixture.root, "raise AssertionError('must not launch')\n")
        worker = fixture.worker(cast(Any, adapter))
        cleanup = claude_cli_capabilities._kill_and_reap

        def failed(process: Any) -> None:
            cleanup(process)
            self.request_stop(worker, job_id, adapter)
            raise RuntimeError("Fictional cleanup uncertainty")

        with (
            patch.object(claude_cli_capabilities, "_kill_and_reap", side_effect=failed),
            patch.object(adapter, "_run_claude_process") as run,
        ):
            self.assertTrue(worker.run_cycle())
        run.assert_not_called()
        self.assert_state(worker, job_id, uncertain=True)
        self.assertEqual(worker.state.get_provider_job(job_id).error_code, "ClaudeCliCleanupError")

    def test_stop_does_not_erase_stream_or_persistence_failure(self) -> None:
        for failure in ("wrong-session", "policy", "persistence"):
            with self.subTest(failure=failure):
                fixture, job_id = self.fixture()
                event = {
                    "type": "assistant",
                    "uuid": fixtures.OTHER_UUID,
                    "parent_tool_use_id": None,
                    "message": {"content": [{"type": "text", "text": "Fictional partial"}]},
                }
                bad = (
                    {"type": "system", "session_id": fixtures.OTHER_UUID}
                    if failure == "wrong-session"
                    else {"type": "control_request"}
                )
                body = (
                    f'event={event!r}\nevent["session_id"]=native\n'
                    f"bad={bad!r}\n"
                    'os.write(1,(json.dumps(event)+"\\n"+json.dumps(bad)+"\\n").encode())\n'
                    "time.sleep(30)\n"
                )
                adapter = self.adapter(fixture.root, body)
                worker = fixture.worker(cast(Any, adapter))
                original = ExecutionJournal.record_claude_item

                def record(journal: Any, *args: Any, **kwargs: Any) -> None:
                    if failure != "persistence":
                        original(journal, *args, **kwargs)
                    self.request_stop(worker, job_id, adapter)
                    if failure == "persistence":
                        raise OSError("Fictional journal failure")

                with patch.object(ExecutionJournal, "record_claude_item", new=record):
                    self.assertTrue(worker.run_cycle())
                self.assert_state(worker, job_id, uncertain=True)
                self.assertIsNone(adapter._active_process)
                checkpoint = ExecutionJournal(worker.state).read(job_id)
                assert checkpoint is not None
                self.assertIsNone(checkpoint["completed_text"])

    def test_late_stop_validates_terminal_stream_before_releasing_root(self) -> None:
        fixture, job_id = self.fixture()
        adapter = self.adapter(
            fixture.root,
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,'
            '"api_error_status":"529","session_id":native,"result":"Contradictory"}))\n',
        )
        worker = fixture.worker(cast(Any, adapter))
        run = adapter._run_claude_process

        def late_stop(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            result = run(*args, **kwargs)
            self.request_stop(worker, job_id, adapter)
            return result

        with patch.object(adapter, "_run_claude_process", side_effect=late_stop):
            self.assertTrue(worker.run_cycle())
        self.assert_state(worker, job_id, uncertain=True)

    def test_first_attempt_completed_before_late_stop_retains_completion(self) -> None:
        fixture, job_id = self.fixture(established=False)
        adapter = self.adapter(
            fixture.root,
            'print(json.dumps({"type":"result","subtype":"success","is_error":False,'
            '"session_id":native,"result":"Fictional final"}))\n',
        )
        worker = fixture.worker(cast(Any, adapter))
        run = adapter._run_claude_process

        def late_stop(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            result = run(*args, **kwargs)
            self.request_stop(worker, job_id, adapter)
            return result

        with patch.object(adapter, "_run_claude_process", side_effect=late_stop):
            self.assertTrue(worker.run_cycle())
        self.assert_state(worker, job_id, uncertain=False)
        checkpoint = ExecutionJournal(worker.state).read(job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["completed_text"], "Fictional final")
