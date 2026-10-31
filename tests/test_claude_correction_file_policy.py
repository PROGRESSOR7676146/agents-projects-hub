"""Owned file-tool composition; native permissions have separate acceptance."""

from __future__ import annotations

import os
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
from hermes_codex_router.claude_mount_pins import MountPinError, MountPins, SandboxLaunch
from hermes_codex_router.claude_stream import parse_claude_stream
from hermes_codex_router.external_runtime import ExternalCliAdapter, ProviderUnavailableError
from tests.test_claude_cli_capabilities import HELP

SESSION = "00000000-0000-4000-8000-000000000001"


class ClaudeCorrectionFilePolicyTests(unittest.TestCase):
    def test_run_turn_composes_real_file_policy_capabilities_and_wrapper(self) -> None:
        # Only the namespace boundary is fictional. The public adapter entry,
        # argv/settings transformation, passive capability process and outer
        # permission wrapper are real; no inference or human grant is involved.
        help_text = HELP.replace("default, dontAsk, plan", "default, dontAsk, manual, plan")
        help_text += "  --input-format <format>  stream-json\n  --replay-user-messages  Replay\n"
        for profile in ("editor", "developer", "workstation"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cli = root / "fictional-claude"
                cli.write_text(
                    f"#!{sys.executable}\n"
                    "import json,os,sys\n"
                    f"help_text={help_text!r}\n"
                    "if sys.argv[1:]==['--help']:\n"
                    " print(help_text); raise SystemExit(0)\n"
                    "argv=sys.argv[1:]\n"
                    "assert argv[-3:]==['--input-format','stream-json','--replay-user-messages']\n"
                    "assert '--' not in argv and '--safe-mode' not in argv\n"
                    "assert argv[argv.index('--permission-mode')+1]=='manual'\n"
                    "settings=json.loads(argv[argv.index('--settings')+1])\n"
                    "assert settings['disableAllHooks'] is False\n"
                    "assert 'PermissionRequest' in settings['hooks']\n"
                    f"assert ('Bash' in argv[argv.index('--tools')+1].split(','))=={profile != 'editor'}\n"
                    "assert os.environ['HUB_CLAUDE_PERMISSION_SOCKET']=='/tmp/example-permission.sock'\n"
                    "assert 'GH_TOKEN' not in os.environ\n"
                    "def emit(value): print(json.dumps(value),flush=True)\n"
                    "first=json.loads(sys.stdin.readline()); emit(dict(first,isReplay=True))\n"
                    "child=json.loads(sys.stdin.readline()); emit(dict(child,isReplay=True))\n"
                    "assert first['message']['content'].endswith('Example first request')\n"
                    "assert child['message']['content']=='Example correction'\n"
                    "emit(dict(type='result',subtype='success',is_error=False,"
                    "session_id=first['session_id'],result='Example complete'))\n"
                    "assert sys.stdin.read()==''\n"
                )
                cli.chmod(0o700)
                launches: list[SandboxLaunch] = []

                def wrap(argv, environment, cwd):
                    self.assertEqual(cwd, root)
                    launch = SandboxLaunch(tuple(argv), dict(environment), MountPins())
                    launches.append(launch)
                    return launch

                sandbox = SimpleNamespace(
                    claude_executable=cli,
                    python_executable=Path(sys.executable),
                    profile=profile,
                    project_root=root,
                    toolchain=None,
                    socket_destination="/tmp/example-permission.sock",
                    wrap=wrap,
                )
                child = ClaudeInputFrame(str(uuid.uuid4()), SESSION, "Example correction")
                prepared: list[ClaudeInputFrame] = []
                receipts: list[tuple[str, int]] = []
                results: list[int] = []
                adapter = ExternalCliAdapter("claude", executable=str(cli))
                with patch.dict(
                    os.environ,
                    {
                        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8327",
                        "ANTHROPIC_AUTH_TOKEN": "example",
                        "GH_TOKEN": "example",
                    },
                    clear=True,
                ):
                    result = adapter.run_turn(
                        cwd=root,
                        prompt="Example first request",
                        new_session_id=SESSION,
                        timeout=10,
                        claude_sandbox=cast(Any, sandbox),
                        claude_corrections=ClaudeCorrectionCallbacks(
                            prepared.append,
                            lambda: child,
                            lambda identifier, boundary: receipts.append((identifier, boundary)),
                            results.append,
                        ),
                    )
                self.assertEqual(result.text, "Example complete")
                self.assertEqual(result.provider_session_id, SESSION)
                self.assertEqual(receipts, [(prepared[0].uuid, 1), (child.uuid, 1)])
                self.assertEqual(results, [1])
                self.assertEqual(len(launches), 1)
                with self.assertRaises(MountPinError):
                    _ = launches[0].pass_fds
                self.assertIsNone(adapter._active_process)

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
