"""Process-start observation is optional evidence, never native acceptance."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from hermes_codex_router.claude_stream import parse_claude_stream
from hermes_codex_router.external_runtime import ExternalCliAdapter, ProviderUnavailableError

SESSION = "00000000-0000-4000-8000-000000000001"


class ClaudeProcessObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = ExternalCliAdapter("claude")
        self.temp = tempfile.TemporaryDirectory(prefix="example-process-observer-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.event = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": SESSION,
            "result": "Fictional saved answer",
        }
        self.argv = (
            sys.executable,
            "-I",
            "-c",
            "import sys; print(sys.argv[1],flush=True)",
            json.dumps(self.event),
        )

    def invoke(self, callback):
        return self.adapter._run_claude_process(
            self.argv,
            cwd=self.root,
            environment={},
            timeout=5,
            expected_session_id=SESSION,
            on_visible_assistant=None,
            on_process_started=callback,
        )

    def test_actual_owned_process_is_registered_before_start_observation(self) -> None:
        observations = []

        def observe() -> None:
            process = self.adapter._active_process
            self.assertIsNotNone(process)
            assert process is not None
            observations.append(process)

        result = self.invoke(observe)
        self.assertEqual(len(observations), 1)
        self.assertEqual(parse_claude_stream(result.stdout).text, self.event["result"])
        self.assertEqual(observations[0].returncode, 0)
        self.assertTrue(observations[0].stdout.closed)
        self.assertTrue(observations[0].stderr.closed)
        self.assertIsNone(self.adapter._active_process)

    def test_failed_popen_never_observes_start(self) -> None:
        observed = []
        with patch("subprocess.Popen", side_effect=FileNotFoundError("fictional")):
            with self.assertRaises(ProviderUnavailableError):
                self.invoke(lambda: observed.append(True))
        self.assertEqual(observed, [])
        self.assertIsNone(self.adapter._active_process)

    def test_file_tool_branch_observes_owned_wrapper_after_preflight(self) -> None:
        # Fictional wrapper proves callback propagation, not namespace isolation.
        launch = MagicMock(argv=self.argv, environment={}, pass_fds=())
        sandbox = SimpleNamespace(claude_executable=Path(sys.executable))
        observations = []

        def preflight(argv, **kwargs):
            self.assertEqual(observations, [])
            self.assertIsNone(self.adapter._active_process)
            self.assertTrue(kwargs["file_tools"])
            return argv

        def observe() -> None:
            launch.__enter__.assert_called_once()
            process = self.adapter._active_process
            assert process is not None
            self.assertEqual(process.args, self.argv)
            observations.append(process)

        with (
            patch("hermes_codex_router.external_runtime.wrap_file_tool_argv", return_value=launch),
            patch.object(self.adapter, "_verified_claude_argv", side_effect=preflight) as verified,
        ):
            result = self.adapter._run_owned_claude_turn(
                self.argv,
                cwd=self.root,
                environment={},
                timeout=5,
                expected_session_id=SESSION,
                on_visible_assistant=None,
                on_process_started=observe,
                sandbox=cast(Any, sandbox),
            )
        verified.assert_called_once()
        launch.__exit__.assert_called_once()
        self.assertEqual(parse_claude_stream(result.stdout).text, self.event["result"])
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].returncode, 0)
        self.assertTrue(observations[0].stdout.closed)
        self.assertTrue(observations[0].stderr.closed)
        self.assertIsNone(self.adapter._active_process)

    def test_optional_callback_failure_preserves_result_and_process_cleanup(self) -> None:
        processes = []

        def failing() -> None:
            processes.append(self.adapter._active_process)
            raise RuntimeError("fictional private diagnostic must not be emitted")

        with patch("hermes_codex_router.external_runtime.survived") as diagnostic:
            result = self.invoke(failing)
        self.assertEqual(parse_claude_stream(result.stdout).text, self.event["result"])
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].returncode, 0)
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)
        self.assertIsNone(self.adapter._active_process)
        self.assertEqual(diagnostic.call_args.args[0], "external_runtime.claude_process_observer")

    def test_buffered_injected_runner_does_not_claim_an_owned_process(self) -> None:
        def fake_run(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, json.dumps(self.event), "")

        adapter = ExternalCliAdapter("claude", run=fake_run)
        observations = []
        with patch.dict(
            "os.environ",
            {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "example"},
            clear=True,
        ):
            result = adapter.run_turn(
                cwd=self.root,
                prompt="Fictional input",
                new_session_id=SESSION,
                on_claude_process_started=lambda: observations.append(True),
            )
        self.assertEqual(result.text, self.event["result"])
        self.assertEqual(observations, [])
