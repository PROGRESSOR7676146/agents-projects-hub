from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.claude_image_input import encode_claude_image_input
from hermes_codex_router.claude_image_receipt import ClaudeImageReceipt
from hermes_codex_router.claude_stream import ClaudeStreamError, VisibleAssistantCallback
from hermes_codex_router.external_runtime import ExternalCliAdapter, ExternalTurnInterrupted
from tests.test_claude_image_input import SESSION, image


class ClaudeInputProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.adapter = ExternalCliAdapter("claude")

    def capture(
        self,
        script: str,
        data: bytes,
        on_visible_assistant: VisibleAssistantCallback | None = None,
        **options: object,
    ):
        return self.adapter._run_claude_process(
            (sys.executable, "-I", "-c", script),
            cwd=self.root,
            environment={"PATH": "/usr/bin:/bin"},
            timeout=options.pop("timeout", 3),  # type: ignore[arg-type]
            expected_session_id=SESSION,
            on_visible_assistant=on_visible_assistant,
            input_data=data,
            **options,  # type: ignore[arg-type]
        )

    def success_script(self, prefix: str = "") -> str:
        return (
            prefix
            + f"""import hashlib,json,sys
data=sys.stdin.buffer.read()
print(json.dumps({{'type':'result','subtype':'success','is_error':False,'session_id':{SESSION!r},'result':hashlib.sha256(data).hexdigest()}}),flush=True)
"""
        )

    def test_backpressure_drains_stdout_stderr_and_preserves_exact_input(self) -> None:
        data = b"fictional-stdin-" * 20000
        script = self.success_script(
            "import os\nos.write(1,b'\\n'*40000)\nos.write(2,b'e'*40000)\n"
        )
        result = self.capture(script, data)
        self.assertIn(hashlib.sha256(data).hexdigest(), result.stdout)

    def test_short_transient_writes_are_retried_without_changing_bytes(self) -> None:
        original = os.write
        calls = 0

        def short(fd: int, data: bytes) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise InterruptedError()
            if calls == 2:
                raise BlockingIOError()
            return original(fd, data[:19])

        data = b"example-byte-input" * 20
        with patch("hermes_codex_router.external_runtime.os.write", side_effect=short):
            result = self.capture(self.success_script(), data)
        self.assertIn(hashlib.sha256(data).hexdigest(), result.stdout)
        self.assertGreater(calls, 10)

    def test_success_marker_cannot_hide_incomplete_input_and_all_pipes_close(self) -> None:
        original = subprocess.Popen
        processes: list[subprocess.Popen[str]] = []

        def track(*args: object, **kwargs: object):
            child = original(*args, **kwargs)  # type: ignore[arg-type]
            processes.append(child)
            return child

        with patch("subprocess.Popen", side_effect=track):
            with self.assertRaisesRegex(ClaudeStreamError, "input transfer incomplete"):
                self.capture(
                    "import os,json;os.close(0);print(json.dumps({'type':'result','subtype':'success','is_error':False,'session_id':"
                    + repr(SESSION)
                    + ",'result':'not proof'}),flush=True)",
                    b"x" * 200000,
                )
        self.assertIsNotNone(processes[0].returncode)
        for pipe in (processes[0].stdin, processes[0].stdout, processes[0].stderr):
            assert pipe is not None
            self.assertTrue(pipe.closed)
        self.assertIsNone(self.adapter._active_process)

    def test_stop_while_input_is_blocked_cleans_owned_process(self) -> None:
        stopped = threading.Event()

        def on_start() -> None:
            def stop() -> None:
                self.adapter.interrupt()
                stopped.set()

            timer = threading.Timer(0.1, stop)
            timer.start()

        with self.assertRaises(ExternalTurnInterrupted):
            self.capture("import time;time.sleep(30)", b"x" * 200000, on_process_started=on_start)
        self.assertTrue(stopped.wait(1))
        self.assertIsNone(self.adapter._active_process)

    @contextmanager
    def assert_process_closed(self):
        original = subprocess.Popen
        processes = []

        def track(*args, **kwargs):
            child = original(*args, **kwargs)
            processes.append(child)
            return child

        with patch("subprocess.Popen", side_effect=track):
            yield
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)
        for pipe in (processes[0].stdin, processes[0].stdout, processes[0].stderr):
            assert pipe is not None
            self.assertTrue(pipe.closed)
        self.assertIsNone(self.adapter._active_process)

    def test_timeout_during_blocked_input_closes_every_pipe(self) -> None:
        with self.assert_process_closed():
            with self.assertRaisesRegex(RuntimeError, "timed out safely"):
                self.capture("import time;time.sleep(30)", b"x" * 200000, timeout=0.1)

    def test_visible_persistence_failure_during_input_closes_every_pipe(self) -> None:
        event = (
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": str(uuid.uuid4()),
                    "session_id": SESSION,
                    "parent_tool_use_id": None,
                    "message": {"content": [{"type": "text", "text": "Example partial"}]},
                }
            )
            + "\n"
        )

        def fail(_item):
            raise RuntimeError("example persistence detail")

        with self.assert_process_closed():
            with self.assertRaisesRegex(ClaudeStreamError, "persistence failed"):
                self.capture(
                    f"import os,time;os.write(1,{event.encode()!r});time.sleep(30)",
                    b"x" * 200000,
                    on_visible_assistant=fail,
                )

    def test_zero_write_cannot_claim_complete_transfer(self) -> None:
        with (
            self.assert_process_closed(),
            patch(
                "hermes_codex_router.external_runtime.os.write",
                return_value=0,
            ),
        ):
            with self.assertRaisesRegex(ClaudeStreamError, "input transfer incomplete"):
                self.capture("import time;time.sleep(30)", b"example-input")

    def test_termination_fault_still_closes_input_and_output(self) -> None:
        original = self.adapter._terminate_claude_process

        def terminate_then_fail(process, *, graceful):
            original(process, graceful=graceful)
            raise RuntimeError("example cleanup fault")

        with (
            self.assert_process_closed(),
            patch.object(
                self.adapter,
                "_terminate_claude_process",
                side_effect=terminate_then_fail,
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "example cleanup fault"):
                self.capture(self.success_script(), b"example-input")

    def test_invalid_receipt_policy_combination_refuses_before_spawn(self) -> None:
        data = encode_claude_image_input("Example", (image(),), SESSION)
        receipt = ClaudeImageReceipt(data, expected_session_id=SESSION)
        with patch("subprocess.Popen") as spawned:
            with self.assertRaises((ClaudeStreamError, RuntimeError)):
                self.capture(
                    self.success_script(), data, image_receipt=receipt, event_policy=lambda _: None
                )
            spawned.assert_not_called()
        self.assertIsNone(self.adapter._active_process)

    def test_stop_or_timeout_while_processed_ack_is_incomplete_closes_process(self) -> None:
        data = encode_claude_image_input("Example", (image(),), SESSION)
        for stopped in (False, True):
            with self.subTest(stopped=stopped):
                self.adapter.prepare_interruptible_turn()
                receipt = ClaudeImageReceipt(data, expected_session_id=SESSION)
                script = 'import sys,time;sys.stdin.buffer.read();sys.stdout.write(\'{\\"type\\":\\"user\\",\');sys.stdout.flush();time.sleep(30)'
                timer = threading.Timer(0.15, self.adapter.interrupt) if stopped else None
                if timer:
                    timer.start()
                try:
                    with self.assert_process_closed():
                        with self.assertRaises(
                            ExternalTurnInterrupted if stopped else RuntimeError
                        ):
                            self.capture(script, data, image_receipt=receipt, timeout=0.3)
                finally:
                    if timer:
                        timer.cancel()
                        timer.join()


if __name__ == "__main__":
    unittest.main()
