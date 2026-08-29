from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.local_transfer import LocalTransferError, local_resume_command


class LocalTransferTests(unittest.TestCase):
    def test_codex_resume_command_is_argument_safe(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="My Project ")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        command = local_resume_command("codex", None, "thread-123", root)
        self.assertEqual(
            command.argv,
            ("codex", "resume", "thread-123", "-C", str(root)),
        )
        self.assertEqual(command.display, f"codex resume thread-123 -C '{root}'")

    def test_external_runtime_commands_resume_same_session(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.assertEqual(
            local_resume_command("opencode", "opencode", "ses_123", root).argv,
            ("opencode", str(root), "--session", "ses_123"),
        )
        antigravity = local_resume_command("antigravity", "agy", "conv-123", root)
        self.assertEqual(
            antigravity.argv,
            ("agy", "--conversation", "conv-123", "--sandbox", "--mode", "accept-edits"),
        )
        self.assertEqual(
            antigravity.display,
            f"cd -- {root} && agy --conversation conv-123 --sandbox --mode accept-edits",
        )

    def test_unsupported_runtime_fails_closed(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with self.assertRaises(LocalTransferError):
            local_resume_command("hermes", None, "session", Path(directory.name))
