"""Developer stream policy joins existing owned-process/worker certainty rules.

The provider is fictional and namespace wrapping is replaced. These tests
establish worker/adapter wiring, not native CLI or installed isolation custody.
"""

from __future__ import annotations

import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.claude_file_sandbox import FileToolSandboxConfig
from hermes_codex_router.claude_mount_pins import MountPins, SandboxLaunch
from hermes_codex_router.claude_permission_host import HostedClaudeLaunch
from hermes_codex_router.execution_journal import ExecutionJournal
from tests import test_claude_native_worker as native_fixtures
from tests import test_claude_stop_precedence as fixtures
from tests.test_claude_cli_capabilities import HELP


class DeveloperStopTests(unittest.TestCase):
    def test_bash_profile_stop_releases_only_proven_outcome_and_never_replays(self) -> None:
        for contradictory in (False, True):
            with self.subTest(contradictory=contradictory):
                controls = fixtures.ClaudeStopPrecedenceTests()
                controls.setUp()
                self.addCleanup(controls.doCleanups)
                fixture, job_id = controls.fixture()
                tool = {
                    "type": "assistant",
                    "uuid": native_fixtures.NATIVE_UUID,
                    "parent_tool_use_id": None,
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_example_bash",
                                "name": "Bash",
                                "input": {"command": "pwd"},
                            }
                        ]
                    },
                }
                visible = {
                    "type": "assistant",
                    "uuid": native_fixtures.OTHER_UUID,
                    "parent_tool_use_id": None,
                    "message": {"content": [{"type": "text", "text": "Fictional partial"}]},
                }
                body = (
                    f"tool={tool!r}\ntool['session_id']=native\n"
                    f"visible={visible!r}\nvisible['session_id']=native\n"
                    "os.write(1,(json.dumps(tool)+'\\n'+json.dumps(visible)+'\\n'"
                    + ("+json.dumps({'type':'control_request'})+'\\n'" if contradictory else "")
                    + ").encode())\ntime.sleep(30)\n"
                )
                adapter = controls.adapter(fixture.root, body)
                child = Path(adapter.executable)
                developer_help = (
                    HELP.replace(
                        "Values: default, dontAsk, plan", "Values: default, dontAsk, plan, manual"
                    )
                    + "  --setting-sources  example option\n"
                )
                child.write_text(child.read_text().replace(repr(HELP), repr(developer_help)))
                worker = fixture.worker(cast(Any, adapter))
                sandbox = cast(
                    FileToolSandboxConfig,
                    SimpleNamespace(
                        profile="developer",
                        python_executable=Path(sys.executable),
                        claude_executable=Path(adapter.executable),
                    ),
                )
                recorded: list[str] = []
                original = ExecutionJournal.record_claude_item

                def record(journal: Any, *args: Any, **kwargs: Any) -> None:
                    original(journal, *args, **kwargs)
                    recorded.append("visible")
                    controls.request_stop(worker, job_id, adapter)

                def wrap(argv, environment, *_args):
                    return SandboxLaunch(tuple(argv), environment, MountPins())

                with (
                    patch(
                        "hermes_codex_router.claude_permission_host.hosted_claude_launch",
                        return_value=nullcontext(HostedClaudeLaunch(sandbox)),
                    ),
                    patch("hermes_codex_router.external_runtime.wrap_file_tool_argv", new=wrap),
                    patch.object(ExecutionJournal, "record_claude_item", new=record),
                ):
                    self.assertTrue(worker.run_cycle())
                self.assertEqual(
                    recorded, ["visible"], worker.state.get_provider_job(job_id).error_code
                )
                controls.assert_state(worker, job_id, uncertain=contradictory)
                self.assertIsNone(adapter._active_process)
