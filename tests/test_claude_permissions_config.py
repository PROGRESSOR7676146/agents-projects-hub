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

    def test_profile_is_editor_unless_developer_is_explicit(self) -> None:
        settings = parse_claude_file_permissions(self.settings())
        assert settings is not None
        self.assertEqual(settings.profile, "editor")
        developer = parse_claude_file_permissions(self.settings() | {"profile": "developer"})
        assert developer is not None
        self.assertEqual(developer.profile, "developer")
        for profile in (None, True, "auto", "Developer", ""):
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                parse_claude_file_permissions(self.settings() | {"profile": profile})

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

    def test_toolchain_is_developer_only_and_retained_for_launch(self) -> None:
        tools = {"roots": ["/opt/example-tools"], "path": ["/opt/example-tools/bin"]}
        with self.assertRaises(ValueError):
            parse_claude_file_permissions(self.settings() | {"toolchain": tools})
        settings = parse_claude_file_permissions(
            self.settings() | {"profile": "developer", "toolchain": tools}
        )
        assert settings is not None and settings.toolchain is not None
        self.assertEqual(str(settings.toolchain.path[0]), "/opt/example-tools/bin")
