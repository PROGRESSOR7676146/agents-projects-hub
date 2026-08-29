from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.terminal import (
    build_codex_remote_argv,
    build_codex_resume_argv,
    terminal_session_name,
)


class TerminalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tempdir.name) / "Example Project"
        self.cwd.mkdir()
        self.socket_path = Path(self.tempdir.name) / "codex.sock"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_remote_tui_attaches_to_same_app_server_thread(self) -> None:
        argv = build_codex_remote_argv(
            socket_path=self.socket_path,
            thread_id="019abcde-1234-7fff-8fff-0123456789ab",
            cwd=self.cwd,
        )
        self.assertEqual(argv[:2], ("codex", "resume"))
        self.assertIn("--remote", argv)
        self.assertIn(f"unix://{self.socket_path}", argv)
        self.assertIn("workspace-write", argv)
        self.assertIn("on-request", argv)

    def test_terminal_name_is_stable_bounded_and_safe(self) -> None:
        name = terminal_session_name("Example Project", "Backend / API", "Codex", 77)
        self.assertEqual(name, "hph-example-project-backend-api-codex-77")
        self.assertLessEqual(len(name), 64)
        self.assertNotIn("/", name)

    def test_stdio_fallback_resumes_the_same_persisted_thread(self) -> None:
        argv = build_codex_resume_argv(
            thread_id="019abcde-1234-7fff-8fff-0123456789ab",
            cwd=self.cwd,
        )
        self.assertEqual(argv[:3], ("codex", "resume", "019abcde-1234-7fff-8fff-0123456789ab"))
        self.assertNotIn("--remote", argv)


if __name__ == "__main__":
    unittest.main()
