"""Runs only inside the empty native namespace of the characterization fixture.

This exercises the product pipe/receipt reader with a fictional Read only. It
does not stand in for the protected permission-host/native editor acceptance.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from unittest.mock import patch


def capture_owned_input(argv, environment, first, release, scenario):
    sys.path.insert(0, "/opt/example/source")
    from hermes_codex_router.claude_corrections import (
        ClaudeCorrectionCallbacks,
        ClaudeCorrectionInput,
        ClaudeInputFrame,
    )
    from hermes_codex_router.claude_native_settings import text_only_settings
    from hermes_codex_router.claude_stream import parse_claude_stream
    from hermes_codex_router.external_runtime import ExternalCliAdapter

    session = "00000000-0000-4000-8000-000000000001"
    identifiers = ("00000000-0000-4000-8000-000000000001", "019abcde-1234-7fff-8fff-0123456789ab")
    initial = "Example first request"
    correction = "Example correction during execution"
    # Use the exact fictional corpus from the enclosing fixture without mounting
    # another test module or introducing any host data.
    actor = sys.modules["__main__"]
    initial, correction = actor.INITIAL, actor.CORRECTION
    child = ClaudeInputFrame(identifiers[1], session, correction)
    frames = []
    receipts = []
    results = []
    written = bytearray()
    raw_write = os.write
    raw_observe = ClaudeCorrectionInput.observe

    def claim():
        return child if first.is_set() else None

    def observe(channel, event):
        frames.append(json.loads(json.dumps(event)))
        return raw_observe(channel, event)

    def write(fd, data):
        count = raw_write(fd, data)
        written.extend(data[:count])
        if child.encode() in written:
            release.set()
        return count

    callbacks = ClaudeCorrectionCallbacks(
        lambda _: None,
        claim,
        lambda identifier, boundary: receipts.append((identifier, boundary)),
        results.append,
    )
    with patch("uuid.uuid4", return_value=uuid.UUID(identifiers[0])):
        channel = ClaudeCorrectionInput(initial, session, callbacks)
    adapter = ExternalCliAdapter("claude")
    argv[argv.index("--settings") + 1] = text_only_settings()
    # The native fixture intentionally allows only Read under dontAsk. The
    # server verifies that exact fictional tool surface; the separate editor
    # fixtures own human permissions and the file-tool event policy.
    with (
        patch.object(ClaudeCorrectionInput, "observe", new=observe),
        patch("os.write", side_effect=write),
    ):
        completed = adapter._run_claude_process(
            tuple(argv),
            cwd=Path("/project"),
            environment=environment,
            timeout=35,
            expected_session_id=session,
            on_visible_assistant=None,
            input_data=channel.initial_data,
            correction_input=channel,
            event_policy=(lambda _: None) if scenario == "tool-loop" else None,
        )
    parse_claude_stream(completed.stdout, expected_session_id=session)
    expected = 1 if scenario == "tool-loop" else 2
    if receipts != [(identifiers[0], 1), (identifiers[1], expected)] or results != list(
        range(1, expected + 1)
    ):
        raise ValueError("example_owned_input_boundary_unproven")
    if not channel.complete or adapter._active_process is not None:
        raise ValueError("example_owned_input_cleanup_unproven")
    return completed.returncode, frames, len(completed.stderr), True
