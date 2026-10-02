from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.claude_stream import (
    ClaudeStreamError,
    ClaudeTerminalFailure,
    ClaudeVisibleAssistant,
)
from hermes_codex_router.external_runtime import (
    ExternalCliAdapter,
    ExternalTurnInterrupted,
    ExternalTurnResult,
    ProviderLimitError,
    ProviderUnavailableError,
)


class ExternalRuntimeTests(unittest.TestCase):
    def _claude_process(
        self, source: str, *, timeout: float = 1, **kwargs: Any
    ) -> ExternalTurnResult:
        """Run a fictional child and verify that every failure reaps its pipes."""
        spawned: list[subprocess.Popen[str]] = []
        original_popen = subprocess.Popen

        def spawn(*args: object, **kwargs: object) -> subprocess.Popen[str]:
            process = original_popen(*args, **kwargs)  # type: ignore[arg-type]
            spawned.append(process)
            return process

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "example"},
                clear=True,
            ),
        ):
            root = Path(directory)
            child = root / "provider"
            child.write_text(f"#!{sys.executable}\n" + source, encoding="utf-8")
            child.chmod(0o700)
            adapter = ExternalCliAdapter("claude", executable=str(child))
            try:
                with patch("subprocess.Popen", side_effect=spawn):
                    return adapter.run_turn(cwd=root, prompt="work", timeout=timeout, **kwargs)
            finally:
                self.assertEqual(len(spawned), 1)
                self.assertIsNotNone(spawned[0].poll())
                self.assertTrue(spawned[0].stdout and spawned[0].stdout.closed)
                self.assertTrue(spawned[0].stderr and spawned[0].stderr.closed)
                self.assertIsNone(adapter._active_process)

    def test_claude_stdout_overflow_is_rejected_before_eof(self) -> None:
        with self.assertRaisesRegex(ClaudeStreamError, "output.*limit"):
            self._claude_process(
                "import os, time\nfor _ in range(600): os.write(1, b'x' * 4096)\ntime.sleep(30)\n"
            )

    def test_claude_stderr_flood_is_rejected_before_eof_without_disclosure(self) -> None:
        with self.assertRaises(ClaudeStreamError) as raised:
            self._claude_process(
                "import os, time\n"
                "for _ in range(600): os.write(2, b'private diagnosis' * 256)\n"
                "time.sleep(30)\n"
            )
        self.assertNotIn("private diagnosis", str(raised.exception))

    def test_claude_malformed_event_is_rejected_before_eof(self) -> None:
        with self.assertRaisesRegex(ClaudeStreamError, "malformed"):
            self._claude_process("import os, time\nos.write(1, b'{broken}\\n')\ntime.sleep(30)\n")

    def test_claude_timeout_reaps_process_and_closes_pipes(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self._claude_process("import time\ntime.sleep(30)\n", timeout=0.05)

    def test_claude_drains_both_pipes_and_preserves_exact_terminal_result(self) -> None:
        session = str(uuid.uuid4())
        message = {
            "type": "assistant",
            "session_id": session,
            "uuid": str(uuid.uuid4()),
            "parent_tool_use_id": None,
            "message": {
                "content": [
                    {"type": "text", "text": "Visible partial é"},
                    {"type": "thinking", "thinking": "private thought"},
                    {"type": "tool_use", "input": {"secret": "private input"}},
                ]
            },
        }
        terminal = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": session,
            "result": "Accepted final",
        }
        chunk = (json.dumps(message, ensure_ascii=False) + "\n" + json.dumps(terminal)).encode()
        visible: list[ClaudeVisibleAssistant] = []
        with patch.object(subprocess.Popen, "communicate", side_effect=AssertionError("unbounded")):
            result = self._claude_process(
                "import os\n"
                "for _ in range(8): os.write(2, b'private diagnostic' * 256)\n"
                f"os.write(1, {chunk!r})\n",
                new_session_id=session,
                on_visible_assistant=visible.append,
            )
        self.assertEqual(result.text, "Accepted final")
        self.assertEqual(result.provider_session_id, session)
        self.assertEqual([item.text for item in visible], ["Visible partial é"])
        self.assertNotIn("private", repr(visible))

    def test_claude_callback_failure_kills_child_without_private_diagnostics(self) -> None:
        session = str(uuid.uuid4())
        event = (
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": str(uuid.uuid4()),
                    "session_id": session,
                    "parent_tool_use_id": None,
                    "message": {"content": [{"type": "text", "text": "Visible"}]},
                }
            )
            + "\n"
        ).encode()

        def fail(_: ClaudeVisibleAssistant) -> None:
            raise RuntimeError("private persistence diagnosis")

        with self.assertRaisesRegex(ClaudeStreamError, "persistence failed") as raised:
            self._claude_process(
                f"import os, time\nos.write(1, {event!r})\ntime.sleep(30)\n",
                new_session_id=session,
                on_visible_assistant=fail,
            )
        self.assertNotIn("private persistence diagnosis", str(raised.exception))

    def test_claude_callback_cannot_accept_result_after_deadline(self) -> None:
        session = str(uuid.uuid4())
        events = [
            {
                "type": "assistant",
                "uuid": str(uuid.uuid4()),
                "session_id": session,
                "parent_tool_use_id": None,
                "message": {"content": [{"type": "text", "text": "Saved partial"}]},
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "session_id": session,
                "result": "Never accepted",
            },
        ]
        chunk = ("\n".join(json.dumps(event) for event in events) + "\n").encode()

        def delay(_: ClaudeVisibleAssistant) -> None:
            time.sleep(0.15)

        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self._claude_process(
                f"import os\nos.write(1, {chunk!r})\n",
                timeout=0.1,
                new_session_id=session,
                on_visible_assistant=delay,
            )

    def test_claude_preexisting_interrupt_prevents_process_launch(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                    "ANTHROPIC_AUTH_TOKEN": "example",
                },
                clear=True,
            ),
            patch("subprocess.Popen") as spawn,
        ):
            adapter = ExternalCliAdapter("claude")
            adapter.prepare_interruptible_turn()
            adapter.interrupt()
            with self.assertRaises(ExternalTurnInterrupted):
                adapter.run_turn(cwd=Path(directory), prompt="work", interrupt_prepared=True)
            spawn.assert_not_called()

    def test_claude_drifting_and_duplicate_terminal_fail_closed(self) -> None:
        session = str(uuid.uuid4())
        terminal = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": session,
            "result": "Never accepted",
        }
        for events in (
            [terminal, terminal],
            [{"type": "system", "session_id": str(uuid.uuid4())}, terminal],
            [terminal, {"type": "assistant", "session_id": session}],
        ):
            chunk = ("\n".join(json.dumps(event) for event in events) + "\n").encode()
            with self.subTest(events=events), self.assertRaises(ClaudeStreamError):
                self._claude_process(
                    f"import os, time\nos.write(1, {chunk!r})\ntime.sleep(30)\n",
                    new_session_id=session,
                )

    def test_claude_interrupt_during_callback_preserves_partial_and_reaps_child(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                    "ANTHROPIC_AUTH_TOKEN": "example",
                },
                clear=True,
            ),
        ):
            root = Path(directory)
            child = root / "provider"
            event = (
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": str(uuid.uuid4()),
                        "session_id": str(uuid.uuid4()),
                        "parent_tool_use_id": None,
                        "message": {"content": [{"type": "text", "text": "Saved partial"}]},
                    }
                )
                + "\n"
            ).encode()
            child.write_text(
                f"#!{sys.executable}\nimport os, signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"os.write(1, {event!r})\ntime.sleep(30)\n",
                encoding="utf-8",
            )
            child.chmod(0o700)
            adapter = ExternalCliAdapter("claude", executable=str(child))
            processes: list[subprocess.Popen[str]] = []
            visible: list[ClaudeVisibleAssistant] = []

            def stop(item: ClaudeVisibleAssistant) -> None:
                visible.append(item)
                assert adapter._active_process is not None
                processes.append(adapter._active_process)
                self.assertTrue(adapter.interrupt())

            with self.assertRaises(ExternalTurnInterrupted):
                adapter.run_turn(cwd=root, prompt="work", timeout=2, on_visible_assistant=stop)
            self.assertEqual([item.text for item in visible], ["Saved partial"])
            self.assertIsNotNone(processes[0].poll())
            self.assertIsNone(adapter._active_process)
            self.assertTrue(processes[0].stdout and processes[0].stdout.closed)
            self.assertTrue(processes[0].stderr and processes[0].stderr.closed)

    def test_claude_timeout_kills_descendant_retaining_pipe_after_leader_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            child_pid = Path(directory) / "descendant-pid"
            source = (
                "import pathlib, subprocess, sys\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid))\n"
            )
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                self._claude_process(source, timeout=0.2)
            self.assertTrue(child_pid.exists())
            pid = int(child_pid.read_text())
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                status = Path(f"/proc/{pid}/stat")
                try:
                    process_state = status.read_text().split()[2]
                except FileNotFoundError:
                    break
                if process_state == "Z":
                    break
                time.sleep(0.01)
            else:
                os.kill(pid, 9)
                self.fail("Claude descendant survived timeout cleanup")

    def test_new_identity_is_rejected_for_every_non_claude_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for runtime in ("gemini", "antigravity", "opencode"):
                with self.subTest(runtime=runtime), self.assertRaises(ProviderUnavailableError):
                    ExternalCliAdapter(runtime).build_argv(
                        cwd=Path(directory), prompt="start", new_session_id=str(uuid.uuid4())
                    )

    def test_invalid_claude_identity_fails_before_provider_invocation(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            adapter = ExternalCliAdapter("claude", run=fake_run)
            for invalid in ("", "not a uuid", 7, [], {}):
                for field in ("resume", "new"):
                    with (
                        self.subTest(field=field, invalid=invalid),
                        self.assertRaises(ProviderUnavailableError),
                    ):
                        adapter.run_turn(
                            cwd=Path(directory),
                            prompt="start",
                            session_id=cast(str, invalid) if field == "resume" else None,
                            new_session_id=cast(str, invalid) if field == "new" else None,
                        )
        self.assertEqual(calls, [])

    def test_claude_caller_chosen_new_identity_is_distinct_from_resume(self) -> None:
        session = str(uuid.uuid4())
        adapter = ExternalCliAdapter("claude")
        with tempfile.TemporaryDirectory() as directory:
            argv = adapter.build_argv(cwd=Path(directory), prompt="start", new_session_id=session)
            self.assertEqual(argv[argv.index("--session-id") + 1], session)
            self.assertNotIn("--resume", argv)
            for kwargs in (
                {"new_session_id": "invalid"},
                {"new_session_id": session, "session_id": session},
            ):
                with self.subTest(kwargs=kwargs), self.assertRaises(ProviderUnavailableError):
                    adapter.build_argv(cwd=Path(directory), prompt="start", **kwargs)

    def test_claude_adapter_verifies_new_identity_and_preserves_terminal_error(self) -> None:
        session = str(uuid.uuid4())
        streams = (
            (
                f'{{"type":"result","subtype":"success","is_error":false,"session_id":"{uuid.uuid4()}","result":"answer"}}',
                0,
                RuntimeError,
            ),
            (
                f'{{"type":"result","subtype":"error_during_execution","is_error":true,"session_id":"{session}","api_error_status":429,"errors":["private"]}}',
                1,
                ClaudeTerminalFailure,
            ),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "example"},
                clear=True,
            ),
        ):
            for stdout, returncode, error_type in streams:

                def fake_run(
                    argv: tuple[str, ...], **_: object
                ) -> subprocess.CompletedProcess[str]:
                    return subprocess.CompletedProcess(argv, returncode, stdout, "private stderr")

                with self.subTest(returncode=returncode), self.assertRaises(error_type):
                    ExternalCliAdapter("claude", run=fake_run).run_turn(
                        cwd=Path(directory), prompt="start", new_session_id=session
                    )

    def test_claude_requires_explicit_local_cpa_route_before_provider_start(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        with tempfile.TemporaryDirectory() as directory:
            adapter = ExternalCliAdapter("claude", run=fake_run)
            for env in (
                {},
                {
                    "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                    "ANTHROPIC_AUTH_TOKEN": "example",
                },
                {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                    "ANTHROPIC_API_KEY": "example",
                    "ANTHROPIC_AUTH_TOKEN": "example",
                },
                {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                    "ANTHROPIC_AUTH_TOKEN": "example",
                    "CLAUDE_CODE_USE_BEDROCK": "1",
                },
            ):
                with self.subTest(env=tuple(env)), patch.dict("os.environ", env, clear=True):
                    with self.assertRaises(ProviderUnavailableError):
                        adapter.run_turn(cwd=Path(directory), prompt="hello")
        self.assertEqual(calls, [])

    def test_claude_start_and_exact_resume_use_only_terminal_visible_result(self) -> None:
        session = str(uuid.uuid4())
        calls: list[tuple[str, ...]] = []

        def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            output = (
                '{"type":"assistant","message":{"content":[{"type":"thinking","thinking":"private"}]}}\n'
                '{"type":"result","subtype":"success","is_error":false,'
                f'"session_id":"{session}","result":"Visible answer"}}\n'
            )
            return subprocess.CompletedProcess(argv, 0, output, "")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "example"},
                clear=True,
            ),
        ):
            adapter = ExternalCliAdapter("claude", run=fake_run)
            started = adapter.run_turn(
                cwd=Path(directory), prompt="hello", model="sonnet", effort="high"
            )
            resumed = adapter.run_turn(cwd=Path(directory), prompt="again", session_id=session)
        self.assertEqual((started.text, resumed.provider_session_id), ("Visible answer", session))
        self.assertNotIn("private", started.text)
        self.assertIn("--strict-mcp-config", calls[0])
        self.assertIn("--safe-mode", calls[0])
        self.assertEqual(calls[0][calls[0].index("--tools") + 1], "")
        self.assertEqual(calls[1][calls[1].index("--resume") + 1], session)
        for call, prompt in zip(calls, ("hello", "again")):
            self.assertEqual(call[-2:], ("--", prompt))

    def test_claude_prompt_cannot_be_read_as_an_option_value(self) -> None:
        session = str(uuid.uuid4())
        calls: list[tuple[str, ...]] = []

        def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            output = (
                '{"type":"result","subtype":"success","is_error":false,'
                f'"session_id":"{session}","result":"Visible answer"}}\n'
            )
            return subprocess.CompletedProcess(argv, 0, output, "")

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "example"},
                clear=True,
            ),
        ):
            ExternalCliAdapter("claude", run=fake_run).run_turn(
                cwd=Path(directory), prompt="- first item"
            )
        # `--tools <tools...>` is variadic; without a separator a prompt that
        # directly follows it would be parsed as another tool name.
        self.assertEqual(calls[0][-4:], ("--tools", "", "--", "- first item"))

    def test_claude_reports_the_model_the_cli_actually_used(self) -> None:
        session = str(uuid.uuid4())
        outputs = (
            (
                '{"type":"system","subtype":"init","model":"claude-example-routed",'
                f'"session_id":"{session}"}}\n'
                '{"type":"assistant","message":{"model":"claude-example-answered",'
                '"content":[{"type":"text","text":"Visible answer"}]}}\n',
                "claude-example-answered",
            ),
            (
                '{"type":"system","subtype":"init","model":"claude-example-routed",'
                f'"session_id":"{session}"}}\n',
                "claude-example-routed",
            ),
            ("", "sonnet"),
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "example"},
                clear=True,
            ),
        ):
            for events, expected in outputs:

                def fake_run(
                    argv: tuple[str, ...], **_: object
                ) -> subprocess.CompletedProcess[str]:
                    output = events + (
                        '{"type":"result","subtype":"success","is_error":false,'
                        f'"session_id":"{session}","result":"Visible answer"}}\n'
                    )
                    return subprocess.CompletedProcess(argv, 0, output, "")

                with self.subTest(expected=expected):
                    result = ExternalCliAdapter("claude", run=fake_run).run_turn(
                        cwd=Path(directory), prompt="hello", model="sonnet"
                    )
                    self.assertEqual(result.model, expected)

    def test_claude_rejects_missing_completion_and_session_switch(self) -> None:
        original = str(uuid.uuid4())
        different = str(uuid.uuid4())
        outputs = (
            '{"type":"assistant","message":{"content":[]}}\n',
            f'{{"type":"result","subtype":"success","is_error":false,"session_id":"{different}","result":"answer"}}\n',
            f'{{"type":"result","subtype":"error","is_error":true,"session_id":"{original}","result":"error"}}\n',
            '{"type":"result", broken\n',
        )
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "example"},
                clear=True,
            ),
        ):
            for output in outputs:

                def fake_run(
                    argv: tuple[str, ...], **_: object
                ) -> subprocess.CompletedProcess[str]:
                    return subprocess.CompletedProcess(argv, 0, output, "")

                with self.subTest(output=output):
                    with self.assertRaises(RuntimeError):
                        ExternalCliAdapter("claude", run=fake_run).run_turn(
                            cwd=Path(directory), prompt="again", session_id=original
                        )

    def test_claude_missing_cli_fails_before_provider_execution(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(
                "os.environ",
                {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "example"},
                clear=True,
            ),
        ):
            adapter = ExternalCliAdapter("claude", executable=str(Path(directory) / "missing-cli"))
            with self.assertRaises(ProviderUnavailableError) as raised:
                adapter.run_turn(cwd=Path(directory), prompt="hello")
        self.assertEqual(raised.exception.code, "claude_cli_unavailable")

    def test_each_cli_adapter_fails_closed_on_incompatible_output(self) -> None:
        def incompatible(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, "human-only output", "")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for runtime in ("gemini", "antigravity", "opencode"):
                with self.subTest(runtime=runtime):
                    adapter = ExternalCliAdapter(runtime, run=incompatible)
                    with self.assertRaisesRegex(
                        RuntimeError, f"{runtime} returned no structured output"
                    ):
                        adapter.run_turn(cwd=root, prompt="work")

    def test_antigravity_surfaces_unsupported_network_location_safely(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "antigravity.log"
            executable = root / "provider"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import pathlib, sys\n"
                "p=pathlib.Path(sys.argv[sys.argv.index('--log-file') + 1])\n"
                "p.write_text('FAILED_PRECONDITION (code 400): User location is not "
                "supported for the API use.\\n')\n"
                'print(\'{"status":"ERROR","error":"Agent execution terminated '
                "due to error.\"}')\n"
                "raise SystemExit(1)\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            adapter = ExternalCliAdapter(
                "antigravity",
                executable=str(executable),
                antigravity_log_path=log,
            )

            with self.assertRaises(ProviderUnavailableError) as raised:
                adapter.run_turn(cwd=root, prompt="work", timeout=5)

        self.assertEqual(raised.exception.code, "unsupported_network_location")
        self.assertNotIn("400", raised.exception.public_message)

    def test_opencode_limit_log_interrupts_cli_that_does_not_exit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "opencode.log"
            log.touch()
            executable = root / "provider"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import pathlib, time\n"
                f"p=pathlib.Path({str(log)!r})\n"
                "p.write_text('Monthly usage limit reached. Resets in 14 days.\\n')\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            adapter = ExternalCliAdapter(
                "opencode",
                executable=str(executable),
                opencode_log_path=log,
            )
            started = time.monotonic()
            with self.assertRaises(ProviderLimitError) as raised:
                adapter.run_turn(cwd=root, prompt="work", timeout=5)

        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(raised.exception.limit.window, "monthly")

    def test_opencode_limit_monitor_ignores_preexisting_log_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "opencode.log"
            log.write_text(
                "Monthly usage limit reached. Resets in 14 days.\n",
                encoding="utf-8",
            )
            executable = root / "provider"
            executable.write_text(
                f"#!{sys.executable}\n"
                'print(\'{"sessionID":"ses-ok","response":"Visible answer"}\')\n',
                encoding="utf-8",
            )
            executable.chmod(0o700)

            result = ExternalCliAdapter(
                "opencode",
                executable=str(executable),
                opencode_log_path=log,
            ).run_turn(cwd=root, prompt="work", timeout=5)

        self.assertEqual(result.text, "Visible answer")

    def test_interrupt_kills_a_provider_that_ignores_sigterm(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "provider"
            ready = root / "ready"
            executable.write_text(
                f"#!{sys.executable}\n"
                "import pathlib, signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                f"pathlib.Path({str(ready)!r}).touch()\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )
            executable.chmod(0o700)
            adapter = ExternalCliAdapter("antigravity", executable=str(executable))
            errors: list[BaseException] = []

            def run() -> None:
                try:
                    adapter.run_turn(cwd=root, prompt="work", timeout=1)
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=run)
            thread.start()
            deadline = time.monotonic() + 0.5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            self.assertTrue(adapter.interrupt())
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ExternalTurnInterrupted)

    def test_interrupt_requested_during_startup_is_not_lost(self) -> None:
        def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, '{"response":"must not surface"}', "")

        with tempfile.TemporaryDirectory() as directory:
            adapter = ExternalCliAdapter("opencode", run=fake_run)
            adapter.prepare_interruptible_turn()
            adapter.interrupt()
            with self.assertRaises(ExternalTurnInterrupted):
                adapter.run_turn(
                    cwd=Path(directory),
                    prompt="work",
                    interrupt_prepared=True,
                )

    def test_gemini_uses_structured_safe_non_yolo_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = ExternalCliAdapter("gemini")
            argv = adapter.build_argv(
                cwd=Path(directory),
                prompt="inspect; touch /tmp/no",
                session_id="session-1",
                model="gemini-model",
            )
        self.assertIn("--output-format", argv)
        self.assertIn("default", argv)
        self.assertNotIn("--yolo", argv)
        self.assertNotIn("yolo", argv)
        self.assertIn("inspect; touch /tmp/no", argv)

    def test_opencode_parses_json_events_without_auto_approval(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            output = (
                '{"sessionID":"ses_1","model":"provider/model"}\n'
                '{"part":{"text":"Visible answer"}}\n'
            )
            return subprocess.CompletedProcess(argv, 0, output, "")

        with tempfile.TemporaryDirectory() as directory:
            result = ExternalCliAdapter("opencode", run=fake_run).run_turn(
                cwd=Path(directory), prompt="hello"
            )
        self.assertEqual(result.provider_session_id, "ses_1")
        self.assertEqual(result.text, "Visible answer")
        self.assertNotIn("--auto", calls[0])

    def test_opencode_effort_is_passed_as_provider_variant(self) -> None:
        adapter = ExternalCliAdapter("opencode", executable="/usr/bin/opencode")
        argv = adapter.build_argv(
            cwd=Path.cwd(),
            prompt="Inspect",
            model="opencode-go/glm-5.3",
            effort="high",
        )
        self.assertIn("--variant", argv)
        self.assertEqual(argv[argv.index("--variant") + 1], "high")

    def test_gemini_profile_is_isolated_in_child_environment(self) -> None:
        environments: list[dict[str, str]] = []

        def fake_run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
            environments.append(dict(kwargs["env"]))  # type: ignore[arg-type]
            return subprocess.CompletedProcess(argv, 0, '{"response":"ok"}', "")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "gemini-account-a"
            profile.mkdir()
            ExternalCliAdapter("gemini", runtime_home=profile, run=fake_run).run_turn(
                cwd=root, prompt="hello"
            )
        self.assertEqual(environments[0]["GEMINI_CLI_HOME"], str(profile.resolve()))

    def test_antigravity_uses_sandboxed_work_mode_and_resumes_conversation(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            output = '{"conversation_id":"conv-1","status":"SUCCESS","response":"Visible answer"}'
            return subprocess.CompletedProcess(argv, 0, output, "")

        with tempfile.TemporaryDirectory() as directory:
            result = ExternalCliAdapter("antigravity", executable="agy", run=fake_run).run_turn(
                cwd=Path(directory), prompt="hello", session_id="conv-1"
            )
        self.assertEqual(result.provider_session_id, "conv-1")
        self.assertEqual(result.text, "Visible answer")
        self.assertIn("--sandbox", calls[0])
        self.assertIn("accept-edits", calls[0])
        self.assertNotIn("plan", calls[0])
        self.assertIn("--conversation", calls[0])
        self.assertNotIn("--dangerously-skip-permissions", calls[0])

    def test_antigravity_effort_is_encoded_in_selected_model(self) -> None:
        adapter = ExternalCliAdapter("antigravity", executable="/usr/bin/agy")
        argv = adapter.build_argv(
            cwd=Path.cwd(),
            prompt="Inspect",
            model="gemini-3.7-flash",
            effort="medium",
        )
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.7-flash-medium")

    def test_antigravity_replaces_existing_effort_suffix(self) -> None:
        adapter = ExternalCliAdapter("antigravity", executable="agy")
        argv = adapter.build_argv(
            cwd=Path.cwd(), prompt="Inspect", model="gemini-3.8-flash-high", effort="low"
        )
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-low")


if __name__ == "__main__":
    unittest.main()
