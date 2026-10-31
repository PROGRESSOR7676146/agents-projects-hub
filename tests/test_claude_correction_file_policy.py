"""Owned file-tool branch and real event policy; fictional wrapper, no isolation claim."""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

from hermes_codex_router.claude_corrections import (
    ClaudeCorrectionCallbacks,
    ClaudeCorrectionInput,
    ClaudeInputFrame,
)
from hermes_codex_router.claude_stream import parse_claude_stream
from hermes_codex_router.external_runtime import ExternalCliAdapter, ProviderUnavailableError

SESSION = "00000000-0000-4000-8000-000000000001"


class ClaudeCorrectionFilePolicyTests(unittest.TestCase):
    def test_owned_file_tool_branch_accepts_bounded_replays_and_lifecycle(self) -> None:
        for profile in ("editor", "developer", "workstation"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                child = ClaudeInputFrame(str(uuid.uuid4()), SESSION, "Example correction")
                receipts = []
                channel = ClaudeCorrectionInput(
                    "🌍" * 200_000,
                    SESSION,
                    ClaudeCorrectionCallbacks(
                        prepare=lambda frame: None,
                        claim=lambda: child,
                        receipt=lambda identifier, boundary: receipts.append(
                            (identifier, boundary)
                        ),
                        result=lambda boundary: None,
                    ),
                )
                code = """
import json,sys
def emit(event): print(json.dumps(event),flush=True)
first=json.loads(sys.stdin.readline())
for state in ('queued','started'):
    emit(dict(type='command_lifecycle',state=state,session_id=first['session_id'],command_uuid=first['uuid'],uuid='example-event'))
emit(dict(first,isReplay=True))
child=json.loads(sys.stdin.readline());emit(dict(child,isReplay=True))
emit(dict(type='result',subtype='success',is_error=False,session_id=first['session_id'],result='Example complete'))
emit(dict(type='command_lifecycle',state='completed',session_id=first['session_id'],command_uuid=first['uuid'],uuid='example-event'))
assert sys.stdin.read()==''
"""
                launch = MagicMock(
                    argv=(sys.executable, "-I", "-c", code), environment={}, pass_fds=()
                )
                sandbox = SimpleNamespace(
                    claude_executable=Path(sys.executable),
                    python_executable=Path(sys.executable),
                    profile=profile,
                )
                adapter = ExternalCliAdapter("claude", executable=sys.executable)
                argv = adapter.build_argv(
                    cwd=root, prompt="Example", new_session_id=SESSION, structured_input=True
                )
                with (
                    patch.object(
                        adapter, "_verified_claude_argv", side_effect=lambda argv, **kwargs: argv
                    ),
                    patch(
                        "hermes_codex_router.external_runtime.wrap_file_tool_argv",
                        return_value=launch,
                    ),
                ):
                    result = adapter._run_owned_claude_turn(
                        argv,
                        cwd=root,
                        environment={},
                        timeout=10,
                        expected_session_id=SESSION,
                        on_visible_assistant=None,
                        on_process_started=None,
                        sandbox=cast(Any, sandbox),
                        input_data=channel.initial_data,
                        correction_input=channel,
                    )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(parse_claude_stream(result.stdout).text, "Example complete")
                self.assertEqual(receipts, [(channel.initial.uuid, 1), (child.uuid, 1)])
                launch.__exit__.assert_called_once()
                self.assertIsNone(adapter._active_process)

    def test_pre_spawn_input_prepare_failure_is_not_execution_uncertainty(self) -> None:
        callbacks = ClaudeCorrectionCallbacks(
            prepare=lambda frame: (_ for _ in ()).throw(RuntimeError("Example prepare fault")),
            claim=lambda: None,
            receipt=lambda identifier, boundary: None,
            result=lambda boundary: None,
        )
        adapter = ExternalCliAdapter("claude")
        with self.assertRaises(ProviderUnavailableError) as raised:
            adapter._prepare_claude_corrections("Example", SESSION, callbacks, False)
        self.assertEqual(raised.exception.code, "claude_stream_input_unverified")
        self.assertIsNone(adapter._active_process)


if __name__ == "__main__":
    unittest.main()
