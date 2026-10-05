from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.socket_directory import ensure_codex_daemon_directory


class CodexSocketDirectoryTests(unittest.TestCase):
    def test_create_private_bind_source_before_daemon_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            directory = ensure_codex_daemon_directory(base=base)
            self.assertEqual(directory, base / f"codex-daemon-{os.getuid()}")
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(ensure_codex_daemon_directory(base=base), directory)

    def test_refuse_symlink_or_unsafe_existing_directory_without_changing_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            directory = base / f"codex-daemon-{os.getuid()}"
            other = base / "other"
            other.mkdir()
            directory.symlink_to(other, target_is_directory=True)
            with self.assertRaises(ValueError):
                ensure_codex_daemon_directory(base=base)
            self.assertTrue(directory.is_symlink())
            directory.unlink()
            directory.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                ensure_codex_daemon_directory(base=base)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o755)


if __name__ == "__main__":
    unittest.main()
