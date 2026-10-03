from __future__ import annotations

import unittest

from hermes_codex_router.claude_permissions_config import parse_claude_file_permissions


class ClaudePermissionsConfigTests(unittest.TestCase):
    def settings(self) -> dict[str, object]:
        return {
            "tlive_config": "/home/example/private/tlive.json",
            "tlive_home": "/home/example/private/tlive",
            "provider_home": "/home/example/provider",
            "runtime_roots": ["/usr/bin", "/opt/example-runtime"],
            "python_executable": "/opt/example-runtime/bin/python",
            "hook_code_root": "/opt/example-runtime/lib",
            "bwrap_executable": "/usr/bin/bwrap",
            "private_paths": ["/home/example/private"],
        }

    def test_disabled_default_and_explicit_paths(self) -> None:
        self.assertIsNone(parse_claude_file_permissions(None))
        settings = parse_claude_file_permissions(self.settings())
        assert settings is not None
        self.assertEqual(str(settings.python_executable), "/opt/example-runtime/bin/python")

    def test_incomplete_config_and_unbounded_mounts_are_refused(self) -> None:
        for change in (
            {"provider_home": "relative"},
            {"provider_home": "/home/example/../private"},
            {"runtime_roots": []},
            {"runtime_roots": ["/usr/bin", "/usr/bin"]},
            {"runtime_roots": [f"/opt/example-{i}" for i in range(33)]},
            {"automatic_approval": True},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                parse_claude_file_permissions(self.settings() | change)
        settings = self.settings()
        del settings["tlive_config"]
        with self.assertRaises(ValueError):
            parse_claude_file_permissions(settings)
