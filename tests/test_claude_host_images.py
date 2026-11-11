"""Images in the streamed correction channel for the unconfined host profile."""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

from hermes_codex_router.claude_corrections import (
    ClaudeCorrectionCallbacks,
    ClaudeCorrectionInput,
    ClaudeInputFrame,
)
from hermes_codex_router.claude_stream import ClaudeStreamError
from hermes_codex_router.external_runtime import ExternalCliAdapter, ProviderUnavailableError
from tests.test_claude_image_input import image

SESSION = "00000000-0000-4000-8000-000000000001"


def callbacks(receipts: list[tuple[str, int]], child: ClaudeInputFrame | None = None):
    return ClaudeCorrectionCallbacks(
        prepare=lambda frame: None,
        claim=lambda: child,
        receipt=lambda identifier, boundary: receipts.append((identifier, boundary)),
        result=lambda boundary: None,
    )


class ClaudeHostImageChannelTests(unittest.TestCase):
    def test_initial_frame_carries_images_and_accepts_their_native_replay(self) -> None:
        receipts: list[tuple[str, int]] = []
        channel = ClaudeCorrectionInput("Example request", SESSION, callbacks(receipts), (image(),))
        self.assertTrue(channel.has_images)
        frame = json.loads(channel.initial_data)
        self.assertEqual(frame["uuid"], channel.initial.uuid)
        kinds = [block["type"] for block in frame["message"]["content"]]
        self.assertEqual(kinds, ["text", "text", "image"])
        replay = {**frame, "isReplay": True}
        self.assertFalse(channel.observe(replay))
        self.assertEqual(receipts, [(channel.initial.uuid, 1)])

    def test_a_replay_that_drops_or_replaces_the_image_is_refused(self) -> None:
        channel = ClaudeCorrectionInput("Example request", SESSION, callbacks([]), (image(),))
        frame = json.loads(channel.initial_data)
        dropped = copy.deepcopy(frame)
        dropped["message"]["content"] = dropped["message"]["content"][:1]
        with self.assertRaises(ClaudeStreamError):
            channel.observe({**dropped, "isReplay": True})

    def test_success_without_the_initial_image_replay_is_refused(self) -> None:
        child = ClaudeInputFrame(str(uuid.uuid4()), SESSION, "Example correction")
        result = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "session_id": SESSION,
            "result": "Example complete",
        }
        channel = ClaudeCorrectionInput("Example", SESSION, callbacks([]), (image(),))
        with self.assertRaises(ClaudeStreamError):
            channel.observe(dict(result))
        channel = ClaudeCorrectionInput("Example", SESSION, callbacks([], child), (image(),))
        self.assertIsNotNone(channel.poll())
        substituted = {
            "type": "user",
            "isReplay": True,
            "uuid": child.uuid,
            "session_id": SESSION,
            "parent_tool_use_id": None,
            "message": {"role": "user", "content": child.text},
        }
        with self.assertRaises(ClaudeStreamError):
            channel.observe(substituted)

    def test_image_channel_event_limit_is_raised_but_still_bounded(self) -> None:
        from hermes_codex_router.claude_stream import (
            MAX_CLAUDE_FILE_EVENT_BYTES,
            MAX_IMAGE_EVENT_BYTES,
            ClaudeStreamReader,
        )

        def reader(images: bool) -> ClaudeStreamReader:
            channel = ClaudeCorrectionInput(
                "Example", SESSION, callbacks([]), (image(),) if images else ()
            )
            return ClaudeStreamReader(
                expected_session_id=SESSION,
                event_policy=lambda _: None,
                correction_input=channel,
            )

        def padded(size: int) -> bytes:
            skeleton = {"type": "system", "subtype": "example", "session_id": SESSION, "pad": ""}
            overhead = len(json.dumps(skeleton).encode())
            return json.dumps({**skeleton, "pad": "a" * (size - overhead)}).encode()

        large = padded(MAX_CLAUDE_FILE_EVENT_BYTES + 1024)
        reader(images=True).feed(large + b"\n")
        with self.assertRaises(ClaudeStreamError):
            reader(images=False).feed(large + b"\n")
        oversized = padded(MAX_IMAGE_EVENT_BYTES + 1)
        with self.assertRaises(ClaudeStreamError):
            reader(images=True).feed(oversized + b"\n")
        with self.assertRaises(ClaudeStreamError):
            reader(images=True).feed(oversized)  # incomplete line, no newline yet

    def test_text_only_channel_is_unchanged(self) -> None:
        channel = ClaudeCorrectionInput("Example request", SESSION, callbacks([]))
        self.assertFalse(channel.has_images)
        self.assertEqual(json.loads(channel.initial_data)["message"]["content"], "Example request")

    def test_images_need_the_host_branch(self) -> None:
        adapter = ExternalCliAdapter("claude")
        with self.assertRaises(ProviderUnavailableError):
            adapter._prepare_claude_corrections("Example", SESSION, callbacks([]), (image(),), None)

    def test_host_branch_runs_an_image_turn_with_a_correction(self) -> None:
        from typing import Any, cast
        from unittest.mock import patch

        from hermes_codex_router.claude_host_profile import ClaudeHostConfig

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "session").mkdir()
            child = ClaudeInputFrame(str(uuid.uuid4()), SESSION, "Example correction")
            receipts: list[tuple[str, int]] = []
            adapter = ExternalCliAdapter("claude", executable=sys.executable)
            host = ClaudeHostConfig(root, root / "session", Path(sys.executable))
            channel = adapter._prepare_claude_corrections(
                "Example request", SESSION, callbacks(receipts, child), (image(),), host
            )
            assert channel is not None
            code = """
import json,sys
def emit(event): print(json.dumps(event),flush=True)
first=json.loads(sys.stdin.readline())
emit(dict(first,isReplay=True))
child=json.loads(sys.stdin.readline());emit(dict(child,isReplay=True))
emit(dict(type='result',subtype='success',is_error=False,session_id=first['session_id'],result='Example complete'))
assert sys.stdin.read()==''
"""
            argv = adapter.build_argv(
                cwd=root, prompt="Example", new_session_id=SESSION, structured_input=True
            )
            environment = dict(os.environ)
            environment.pop("INVOCATION_ID", None)
            with (
                patch.dict(os.environ, {}, clear=False),
                patch.object(
                    adapter,
                    "_verified_claude_argv",
                    side_effect=lambda argv, **kwargs: (sys.executable, "-I", "-c", code),
                ),
            ):
                os.environ.pop("INVOCATION_ID", None)
                result = adapter._run_owned_claude_turn(
                    argv,
                    cwd=root,
                    environment=environment,
                    timeout=10,
                    expected_session_id=SESSION,
                    on_visible_assistant=None,
                    on_process_started=None,
                    sandbox=cast(Any, host),
                    input_data=channel.initial_data,
                    correction_input=channel,
                )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(receipts, [(channel.initial.uuid, 1), (child.uuid, 1)])


class ClaudeHostImageConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests import test_claude_image_controls as fixtures

        self.fixture = fixtures.ClaudeImageConfigurationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_host_images_need_streamed_input_and_confined_profiles_still_refuse(self) -> None:
        from hermes_codex_router.hub_config import HubConfigError

        host = {"profile": "host", "provider_home": "/home/example/claude-sessions"}
        config = self.fixture.load(
            claude_stream_input=True, claude_image_input=True, claude_file_permissions=host
        )
        self.assertTrue(config.claude_image_input and config.claude_stream_input)
        for refused in (
            dict(claude_image_input=True, claude_file_permissions=host),
            dict(claude_stream_input=True, claude_image_input=True),
        ):
            with self.subTest(refused=refused), self.assertRaises(HubConfigError):
                self.fixture.load(**refused)


if __name__ == "__main__":
    unittest.main()
