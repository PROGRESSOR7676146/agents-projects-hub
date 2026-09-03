from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from hermes_codex_router.external_runtime import (
    ExternalCliAdapter,
    ExternalTurnInterrupted,
    ProviderLimitError,
    ProviderUnavailableError,
)


class ExternalRuntimeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
