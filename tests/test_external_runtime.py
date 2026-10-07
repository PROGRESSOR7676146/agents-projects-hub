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
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.claude_file_policy import require_file_tool_event
from hermes_codex_router.claude_file_sandbox import FileToolSandboxConfig, FileToolSandboxError
from hermes_codex_router.claude_mount_pins import MountPins, SandboxLaunch
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
from hermes_codex_router.owned_process_exit import peek_exit_code
from tests.test_claude_cli_capabilities import HELP, LeaderExitClock


def fictional_claude_source(source: str) -> str:
    return (
        f"#!{sys.executable}\nimport sys\n"
        f"if sys.argv[1:] == ['--help']:\n    print({HELP!r})\n    sys.exit(0)\n" + source
    )


class ExternalRuntimeTests(unittest.TestCase):
    def test_hosted_file_tools_require_owned_runner_and_never_fall_back(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        sandbox = cast(
            FileToolSandboxConfig,
            SimpleNamespace(
                python_executable=Path("/usr/bin/python3"), claude_executable=Path("/usr/bin/true")
            ),
        )
        with patch.dict(
            os.environ,
            {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "example"},
            clear=True,
        ):
            adapter = ExternalCliAdapter(
                "claude", run=lambda *args, **kwargs: subprocess.CompletedProcess([], 0, "", "")
            )
            with self.assertRaises(ProviderUnavailableError) as failure:
                adapter.run_turn(cwd=root, prompt="Example", claude_sandbox=sandbox)
            self.assertEqual(failure.exception.code, "claude_permission_host_unverified")
            adapter = ExternalCliAdapter("claude")
            with (
                patch.object(
                    adapter, "_verified_claude_argv", side_effect=lambda argv, **kwargs: argv
                ),
                patch(
                    "hermes_codex_router.external_runtime.wrap_file_tool_argv",
                    side_effect=FileToolSandboxError("example"),
                ),
                patch.object(adapter, "_run_claude_process") as native,
                self.assertRaises(ProviderUnavailableError),
            ):
                adapter.run_turn(cwd=root, prompt="Example", claude_sandbox=sandbox)
            native.assert_not_called()

    def test_hosted_file_tools_wire_fixed_policy_to_owned_process(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        native_id = str(uuid.uuid4())
        terminal = (
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": native_id,
                    "result": "Example",
                }
            )
            + "\n"
        )
        sandbox = cast(
            FileToolSandboxConfig,
            SimpleNamespace(
                python_executable=Path("/usr/bin/python3"), claude_executable=Path("/usr/bin/true")
            ),
        )
        adapter = ExternalCliAdapter("claude")
        pins = MountPins()
        descriptor = pins.open(root, directory=True)
        launch = SandboxLaunch(("example-boundary",), {"EXAMPLE": "isolated"}, pins)
        with (
            patch.dict(
                os.environ,
                {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "example"},
                clear=True,
            ),
            patch.object(
                adapter, "_verified_claude_argv", side_effect=lambda argv, **kwargs: argv
            ) as verify,
            patch(
                "hermes_codex_router.external_runtime.wrap_file_tool_argv",
                return_value=launch,
            ) as wrap,
            patch.object(
                adapter,
                "_run_claude_process",
                return_value=subprocess.CompletedProcess([], 0, terminal, ""),
            ) as native,
        ):
            adapter.run_turn(
                cwd=root, prompt="Example", claude_sandbox=sandbox, new_session_id=native_id
            )
        self.assertTrue(verify.call_args.kwargs["file_tools"])
        argv = wrap.call_args.args[0]
        self.assertNotIn("--safe-mode", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read,Glob,Grep,Write,Edit")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "")
        self.assertEqual(native.call_args.args[0], ("example-boundary",))
        self.assertIs(native.call_args.kwargs["event_policy"], require_file_tool_event)
        self.assertEqual(native.call_args.kwargs["pass_fds"], (descriptor,))
        self.assertEqual(native.call_args.kwargs["cwd"], Path("/"))
        with self.assertRaises(OSError):
            os.fstat(descriptor)

    def test_hosted_mount_descriptors_close_on_every_owned_runner_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-native-pins-") as directory:
            root = Path(directory)
            sandbox = cast(
                FileToolSandboxConfig,
                SimpleNamespace(
                    python_executable=Path("/usr/bin/python3"),
                    claude_executable=Path("/usr/bin/true"),
                ),
            )
            for failure in (
                ExternalTurnInterrupted("example stop"),
                ProviderUnavailableError("example", "example unavailable"),
                subprocess.TimeoutExpired("example", 1),
                ClaudeStreamError("example malformed output"),
            ):
                with self.subTest(failure=type(failure).__name__):
                    pins = MountPins()
                    descriptor = pins.open(root, directory=True)
                    launch = SandboxLaunch(("example-boundary",), {}, pins)
                    adapter = ExternalCliAdapter("claude")
                    with (
                        patch.dict(
                            os.environ,
                            {
                                "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                                "ANTHROPIC_AUTH_TOKEN": "example",
                            },
                            clear=True,
                        ),
                        patch.object(
                            adapter,
                            "_verified_claude_argv",
                            side_effect=lambda argv, **kwargs: argv,
                        ),
                        patch(
                            "hermes_codex_router.external_runtime.wrap_file_tool_argv",
                            return_value=launch,
                        ),
                        patch.object(adapter, "_run_claude_process", side_effect=failure),
                        self.assertRaises(type(failure)),
                    ):
                        adapter.run_turn(cwd=root, prompt="Example", claude_sandbox=sandbox)
                    with self.assertRaises(OSError):
                        os.fstat(descriptor)

    def test_hosted_popen_failure_closes_pins_and_has_no_plain_retry(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-spawn-pins-") as directory:
            root = Path(directory)
            sandbox = cast(
                FileToolSandboxConfig,
                SimpleNamespace(
                    python_executable=Path("/usr/bin/python3"),
                    claude_executable=Path("/usr/bin/true"),
                ),
            )
            pins = MountPins()
            descriptor = pins.open(root, directory=True)
            launch = SandboxLaunch(("example-boundary",), {}, pins)
            adapter = ExternalCliAdapter("claude")
            with (
                patch.dict(
                    os.environ,
                    {
                        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                        "ANTHROPIC_AUTH_TOKEN": "example",
                    },
                    clear=True,
                ),
                patch.object(
                    adapter, "_verified_claude_argv", side_effect=lambda argv, **kwargs: argv
                ),
                patch(
                    "hermes_codex_router.external_runtime.wrap_file_tool_argv", return_value=launch
                ),
                patch("subprocess.Popen", side_effect=OSError("example spawn failure")) as spawn,
                self.assertRaises(ProviderUnavailableError),
            ):
                adapter.run_turn(cwd=root, prompt="Example", claude_sandbox=sandbox)
            spawn.assert_called_once()
            self.assertTrue(spawn.call_args.kwargs["close_fds"])
            self.assertEqual(spawn.call_args.kwargs["pass_fds"], (descriptor,))
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_hosted_capability_probe_uses_validated_executable_after_mount_preflight(self) -> None:
        with tempfile.TemporaryDirectory(prefix="example-probe-pins-") as directory:
            root = Path(directory)
            executable = Path("/usr/bin/true")
            sandbox = cast(
                FileToolSandboxConfig,
                SimpleNamespace(
                    python_executable=Path("/usr/bin/python3"),
                    claude_executable=executable,
                ),
            )
            pins = MountPins()
            descriptor = pins.open(root, directory=True)
            launch = SandboxLaunch(("example-boundary",), {}, pins)
            adapter = ExternalCliAdapter("claude", executable="example-untrusted-path-entry")
            native_id = str(uuid.uuid4())
            terminal = (
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "session_id": native_id,
                        "result": "Example",
                    }
                )
                + "\n"
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                        "ANTHROPIC_AUTH_TOKEN": "example",
                    },
                    clear=True,
                ),
                patch.object(
                    adapter._claude_capabilities, "require", return_value=str(executable)
                ) as probe,
                patch("hermes_codex_router.external_runtime.wrap_file_tool_argv") as wrap,
                patch.object(
                    adapter,
                    "_run_claude_process",
                    return_value=subprocess.CompletedProcess([], 0, terminal, ""),
                ),
            ):

                def validated(argv: tuple[str, ...], *_args: object) -> SandboxLaunch:
                    probe.assert_not_called()
                    self.assertEqual(argv[0], str(executable))
                    return launch

                wrap.side_effect = validated
                adapter.run_turn(
                    cwd=root, prompt="Example", claude_sandbox=sandbox, new_session_id=native_id
                )
            self.assertEqual(probe.call_args.args[0], str(executable))
            with self.assertRaises(OSError):
                os.fstat(descriptor)

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
            child.write_text(fictional_claude_source(source), encoding="utf-8")
            child.chmod(0o700)
            adapter = ExternalCliAdapter("claude", executable=str(child))
            try:
                with patch("subprocess.Popen", side_effect=spawn):
                    return adapter.run_turn(cwd=root, prompt="work", timeout=timeout, **kwargs)
            finally:
                self.assertEqual(len(spawned), 2)
                for process in spawned:
                    self.assertIsNotNone(process.poll())
                    self.assertTrue(process.stdout and process.stdout.closed)
                    self.assertTrue(process.stderr and process.stderr.closed)
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

    def test_claude_unexpected_permission_request_is_refused_before_eof(self) -> None:
        visible: list[ClaudeVisibleAssistant] = []
        request = (
            json.dumps(
                {
                    "type": "control_request",
                    "request": {"subtype": "can_use_tool", "input": "private command"},
                }
            )
            + "\n"
        )
        with self.assertRaisesRegex(ClaudeStreamError, "text-only") as raised:
            self._claude_process(
                f"import os, time\nos.write(1, {request.encode()!r})\ntime.sleep(30)\n",
                on_visible_assistant=visible.append,
            )
        self.assertEqual(visible, [])
        self.assertNotIn("private command", str(raised.exception))

    def test_claude_timeout_reaps_process_and_closes_pipes(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self._claude_process("import time\ntime.sleep(30)\n", timeout=0.05)

    def test_claude_help_and_turn_reserve_leader_pid_until_last_group_signal(self) -> None:
        session = str(uuid.uuid4())
        terminal = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": session,
            "result": "Visible answer",
        }
        outcomes: list[int] = []
        real_killpg = os.killpg

        def signal_reserved_group(pid: int, requested_signal: int) -> None:
            # A reaped leader raises ChildProcessError here, before any signal
            # could reach a recycled group. WNOWAIT preserves the reservation.
            outcome = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            self.assertIsNotNone(outcome)
            assert outcome is not None
            outcomes.append(outcome.si_code)
            real_killpg(pid, requested_signal)

        with patch("os.killpg", side_effect=signal_reserved_group):
            result = self._claude_process(
                f"import json\nprint(json.dumps({terminal!r}))\n",
                new_session_id=session,
            )
        self.assertEqual(result.text, "Visible answer")
        self.assertEqual(outcomes, [os.CLD_EXITED, os.CLD_EXITED])

    def test_claude_stop_observation_and_signal_cannot_race_cleanup_reaping(self) -> None:
        adapter = ExternalCliAdapter("claude")
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True, text=True
        )
        adapter._active_process = process
        peeked = threading.Event()
        release_peek = threading.Event()
        cleanup_attempted = threading.Event()
        mutex = threading.Lock()
        errors: list[BaseException] = []
        stopped: list[bool] = []
        signals: list[int] = []
        real_killpg = os.killpg

        class ObservedLock:
            def __enter__(self) -> None:
                if threading.current_thread().name == "cleanup":
                    cleanup_attempted.set()
                mutex.acquire()

            def __exit__(self, *_: object) -> None:
                mutex.release()

        adapter._process_lock = cast(Any, ObservedLock())

        def pause_stop_observation(child: subprocess.Popen[str]) -> int | None:
            code = peek_exit_code(child)
            if threading.current_thread().name == "stopper":
                self.assertIsNone(code)
                peeked.set()
                self.assertTrue(release_peek.wait(10))
            return code

        def signal_waitable_group(pid: int, requested_signal: int) -> None:
            # No signal may follow waitpid, including a concurrent /stop.
            os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            signals.append(requested_signal)
            real_killpg(pid, requested_signal)

        def stop() -> None:
            try:
                stopped.append(adapter.interrupt())
            except BaseException as error:
                errors.append(error)

        def cleanup() -> None:
            try:
                adapter._terminate_claude_process(process, graceful=False)
            except BaseException as error:
                errors.append(error)

        stopper = threading.Thread(target=stop, name="stopper")
        cleaner = threading.Thread(target=cleanup, name="cleanup")
        with (
            patch("hermes_codex_router.external_runtime.peek_exit_code", pause_stop_observation),
            patch("os.killpg", signal_waitable_group),
        ):
            try:
                stopper.start()
                self.assertTrue(peeked.wait(10))
                cleaner.start()
                self.assertTrue(cleanup_attempted.wait(10))
                self.assertEqual(signals, [])
            finally:
                release_peek.set()
                stopper.join(10)
                if cleaner.ident is not None:
                    cleaner.join(10)
                if process.returncode is None:
                    real_killpg(process.pid, 9)
                    process.wait(timeout=5)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(cleaner.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(stopped, [True])
        self.assertEqual(signals, [9, 9])

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
                fictional_claude_source(
                    "import os, signal, time\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    f"os.write(1, {event!r})\ntime.sleep(30)\n"
                ),
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
                "import os, pathlib, subprocess, sys\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
                f"marker = pathlib.Path({str(child_pid)!r})\n"
                "ready = marker.with_suffix('.ready')\n"
                "ready.write_text(f'{child.pid} {os.getpid()}')\n"
                "ready.replace(marker)\n"
            )
            clock = LeaderExitClock(child_pid)
            with (
                patch(
                    "hermes_codex_router.external_runtime.time",
                    SimpleNamespace(monotonic=clock.monotonic),
                ),
                self.assertRaisesRegex(RuntimeError, "timed out"),
            ):
                self._claude_process(source, timeout=0.2)
            self.assertTrue(child_pid.exists())
            self.assertTrue(clock.observed_leader_exit)
            pid = int(child_pid.read_text().split()[0])
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                status = Path(f"/proc/{pid}/stat")
                try:
                    process_state = status.read_text().split()[2]
                except (FileNotFoundError, ProcessLookupError):
                    # Linux can reap the descendant after opening /proc/stat
                    # but before read(), which reports ESRCH rather than ENOENT.
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
            (
                f'{{"type":"result","subtype":"success","is_error":true,"session_id":"{session}","api_error_status":529,"result":"private"}}',
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
        for call in calls:
            settings = json.loads(call[call.index("--settings") + 1])
            self.assertIs(settings["disableAllHooks"], True)
            self.assertEqual(
                settings["enabledPlugins"],
                {
                    "cc-plugin-agents-md@builtin": False,
                    "cc-plugin-diff@builtin": False,
                    "cc-plugin-plugin-authoring@builtin": False,
                    "cc-plugin-telemetry@builtin": False,
                },
            )
            self.assertNotIn("cc-plugin-sec-default@builtin", settings["enabledPlugins"])
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
