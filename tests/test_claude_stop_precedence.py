"""Real owned CLI/worker stop races must preserve contradictory evidence."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router import claude_cli_capabilities
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.external_runtime import ExternalCliAdapter
from hermes_codex_router.owned_process_exit import peek_exit_code
from hermes_codex_router.root_blockers import persistent_root_blocker
from tests import test_claude_native_worker as fixtures
from tests.native_process_capture import owned_fixture_process
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

    def test_directory_cleanup_failure_alone_cannot_establish_stop_proof(self) -> None:
        fixture, job_id = self.fixture()
        adapter = self.adapter(fixture.root, "raise AssertionError('must not launch')\n")
        worker = fixture.worker(cast(Any, adapter))
        temporary_directory = claude_cli_capabilities.tempfile.TemporaryDirectory

        class FailingDirectory(temporary_directory):
            def cleanup(inner) -> None:
                super().cleanup()
                self.request_stop(worker, job_id, adapter)
                raise OSError("Fictional directory cleanup failure")

        with (
            patch.object(claude_cli_capabilities.tempfile, "TemporaryDirectory", FailingDirectory),
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
            try:
                return run(*args, **kwargs)
            finally:
                self.request_stop(worker, job_id, adapter)

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
            try:
                return run(*args, **kwargs)
            finally:
                self.request_stop(worker, job_id, adapter)

        with patch.object(adapter, "_run_claude_process", side_effect=late_stop):
            self.assertTrue(worker.run_cycle())
        self.assert_state(worker, job_id, uncertain=False)
        checkpoint = ExecutionJournal(worker.state).read(job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["completed_text"], "Fictional final")

    def test_killed_partial_terminal_line_is_not_completion_proof(self) -> None:
        for established in (False, True):
            with self.subTest(established=established):
                fixture, job_id = self.fixture(established=established)
                event = {
                    "type": "assistant",
                    "uuid": fixtures.OTHER_UUID,
                    "parent_tool_use_id": None,
                    "message": {"content": [{"type": "text", "text": "Fictional partial"}]},
                }
                partial = '{"type":"res'
                adapter = self.adapter(
                    fixture.root,
                    f'event={event!r}\nevent["session_id"]=native\n'
                    f'os.write(1,(json.dumps(event)+"\\n"+{partial!r}).encode())\n'
                    "time.sleep(30)\n",
                )
                worker = fixture.worker(cast(Any, adapter))
                original = ExecutionJournal.record_claude_item

                def record(journal: Any, *args: Any, **kwargs: Any) -> None:
                    original(journal, *args, **kwargs)
                    self.request_stop(worker, job_id, adapter)

                with patch.object(ExecutionJournal, "record_claude_item", new=record):
                    self.assertTrue(worker.run_cycle())
                self.assert_state(worker, job_id, uncertain=not established)
                checkpoint = ExecutionJournal(worker.state).read(job_id)
                assert checkpoint is not None
                self.assertIsNone(checkpoint["completed_text"])

    def test_stop_during_terminal_drain_preserves_completed_first_attempt(self) -> None:
        for outcome in ("success", "no-newline"):
            with self.subTest(outcome=outcome):
                self._stop_while_draining_terminal(established=False, outcome=outcome)

    def test_stop_during_terminal_drain_preserves_completed_session(self) -> None:
        self._stop_while_draining_terminal(established=True, outcome="success")

    def test_stop_during_terminal_drain_keeps_conflicts_uncertain(self) -> None:
        for outcome in ("status", "unknown", "empty", "error-flag", "exit"):
            with self.subTest(outcome=outcome):
                self._stop_while_draining_terminal(established=True, outcome=outcome)

    def test_late_stop_cannot_hide_crash_or_unterminated_final_event(self) -> None:
        for outcome in ("crash", "success", "status", "unknown", "foreign", "policy"):
            with self.subTest(outcome=outcome):
                self._stop_while_draining_terminal(
                    established=True, outcome=outcome, ending="", exit_code=1
                )

    def test_killed_stream_still_validates_complete_unterminated_final_event(self) -> None:
        for outcome in ("success", "status", "foreign", "policy", "malformed"):
            with self.subTest(outcome=outcome):
                self._stop_while_draining_terminal(
                    established=True, outcome=outcome, ending="", running=True
                )

    def _stop_while_draining_terminal(
        self,
        *,
        established: bool,
        outcome: str,
        ending: str | None = None,
        exit_code: int | None = None,
        running: bool = False,
    ) -> None:
        fixture, job_id = self.fixture(established=established)
        event = {
            "type": "assistant",
            "uuid": fixtures.OTHER_UUID,
            "parent_tool_use_id": None,
            "message": {"content": [{"type": "text", "text": "Fictional partial"}]},
        }
        terminal: dict[str, Any] = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Fictional final",
        }
        if outcome == "status":
            terminal["api_error_status"] = "529"
        elif outcome == "unknown":
            terminal["subtype"] = "unknown"
        elif outcome == "empty":
            terminal["result"] = ""
        elif outcome == "error-flag":
            terminal.update(subtype="error_during_execution", is_error=False)
        elif outcome == "policy":
            terminal = {"type": "control_request"}
        if ending is None:
            ending = "" if outcome == "no-newline" else "\n"
        if exit_code is None:
            exit_code = 1 if outcome == "exit" else 0
        written = fixture.root / "fictional-output-written"
        body = (
            f'event={event!r}\nevent["session_id"]=native\n'
            f'terminal={terminal!r}\nterminal["session_id"]=native\n'
            + (f'terminal["session_id"]={fixtures.OTHER_UUID!r}\n' if outcome == "foreign" else "")
            + (
                'os.write(1,(json.dumps(event)+"\\n").encode())\n'
                if outcome == "crash"
                else 'os.write(1,(json.dumps(event)+"\\n{bad}").encode())\n'
                if outcome == "malformed"
                else f'os.write(1,(json.dumps(event)+"\\n"+json.dumps(terminal)+{ending!r}).encode())\n'
            )
            + f"open({str(written)!r},'w').close()\n"
            + ("time.sleep(30)\n" if running else f"sys.exit({exit_code})\n")
        )
        adapter = self.adapter(fixture.root, body)
        worker = fixture.worker(cast(Any, adapter))
        original = ExecutionJournal.record_claude_item
        stopped = []

        def record(journal: Any, *args: Any, **kwargs: Any) -> None:
            original(journal, *args, **kwargs)
            assert adapter._active_process is not None
            deadline = time.monotonic() + 5
            while not written.exists() or (
                not running and peek_exit_code(adapter._active_process) is None
            ):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.001)
            self.request_stop(worker, job_id, adapter)
            stopped.append(True)

        with patch.object(ExecutionJournal, "record_claude_item", new=record):
            self.assertTrue(worker.run_cycle())
        self.assertEqual(stopped, [True])
        completed = outcome in {"success", "no-newline"} and exit_code == 0 and not running
        self.assert_state(worker, job_id, uncertain=not completed)
        self.assertIsNone(adapter._active_process)
        checkpoint = ExecutionJournal(worker.state).read(job_id)
        assert checkpoint is not None
        self.assertEqual(checkpoint["completed_text"], "Fictional final" if completed else None)

    def test_stop_drain_timeout_keeps_outside_group_pipe_holder_uncertain(self) -> None:
        fixture, job_id = self.fixture()
        event = {
            "type": "assistant",
            "uuid": fixtures.OTHER_UUID,
            "parent_tool_use_id": None,
            "message": {"content": [{"type": "text", "text": "Fictional partial"}]},
        }
        adapter = self.adapter(
            fixture.root,
            f'event={event!r}\nevent["session_id"]=native\n'
            'os.write(1,(json.dumps(event)+"\\n").encode())\n'
            "time.sleep(30)\n",
        )
        worker = fixture.worker(cast(Any, adapter))
        original = ExecutionJournal.record_claude_item
        holders = []

        def record(journal: Any, *args: Any, **kwargs: Any) -> None:
            original(journal, *args, **kwargs)
            process = adapter._active_process
            assert process is not None and process.stdout is not None
            # The fixture owns this exact live CLI. Duplicate its pipe writer
            # into a separately owned child, reproducing a setsid pipe holder
            # without orphaning a process or relying on optional pidfd APIs.
            writer = os.open(f"/proc/{process.pid}/fd/1", os.O_WRONLY)
            try:
                expected = os.fstat(process.stdout.fileno())
                observed = os.fstat(writer)
                self.assertEqual(
                    (observed.st_dev, observed.st_ino), (expected.st_dev, expected.st_ino)
                )
                holder = self.enterContext(
                    owned_fixture_process(
                        [sys.executable, "-I", "-c", "import time; time.sleep(60)"],
                        {},
                        pass_fds=(writer,),
                    )
                )
                holders.append(holder)
            finally:
                os.close(writer)
            self.request_stop(worker, job_id, adapter)

        with patch.object(ExecutionJournal, "record_claude_item", new=record):
            self.assertTrue(worker.run_cycle())
        self.assertEqual(len(holders), 1)
        self.assertIsNone(peek_exit_code(holders[0]))
        self.assert_state(worker, job_id, uncertain=True)
        self.assertIsNone(adapter._active_process)

    def test_stop_reap_timeout_preserves_uncertainty_with_buffered_output(self) -> None:
        fixture, job_id = self.fixture()
        marker = fixture.root / "fictional-buffered"
        adapter = self.adapter(
            fixture.root,
            "from pathlib import Path\n"
            f'os.write(1,(json.dumps({{"type":"system","session_id":{fixtures.OTHER_UUID!r}}})+"\\n").encode())\n'
            f"Path({str(marker)!r}).touch()\n"
            "time.sleep(30)\n",
        )
        worker = fixture.worker(cast(Any, adapter))
        run = adapter._run_claude_process
        terminate = adapter._terminate_claude_process
        calls = []

        def run_with_stop(*args: Any, **kwargs: Any) -> Any:
            started = kwargs.get("on_process_started")

            def buffered() -> None:
                if started is not None:
                    started()
                deadline = time.monotonic() + 5
                while not marker.exists():
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.001)
                self.request_stop(worker, job_id, adapter)

            kwargs["on_process_started"] = buffered
            return run(*args, **kwargs)

        def slow_reap(process: Any, *, graceful: bool) -> None:
            calls.append(graceful)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("fictional-cleanup", 5)
            terminate(process, graceful=graceful)

        with (
            patch.object(adapter, "_run_claude_process", side_effect=run_with_stop),
            patch.object(adapter, "_terminate_claude_process", side_effect=slow_reap),
        ):
            self.assertTrue(worker.run_cycle())
        self.assertTrue(calls)
        self.assert_state(worker, job_id, uncertain=True)
        self.assertIsNone(adapter._active_process)
