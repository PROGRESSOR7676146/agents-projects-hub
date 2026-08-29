from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.hub_config import HubConfigError, load_hub_config


class HubConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.token = self.base / "codex-token"
        self.token.write_text("123456:secret-token-value", encoding="utf-8")
        self.token.chmod(0o600)
        self.registry = self.base / "projects.json"
        self.registry.write_text(
            json.dumps({"schema_version": 1, "allowed_roots": [], "projects": []}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_config(self, **overrides: object) -> Path:
        document = {
            "schema_version": 1,
            "owner_user_ids": [123456789],
            "registry_path": str(self.registry),
            "state_path": str(self.base / "state.db"),
            "projects": [{"project_id": "pythia", "telegram_chat_id": -1001234567890}],
            "agents": [
                {
                    "agent_id": "codex",
                    "display_name": "Codex",
                    "telegram_username": "project_codex_bot",
                    "runtime": "codex",
                    "token_file": str(self.token),
                    "terminal_enabled": True,
                },
                {
                    "agent_id": "hermes",
                    "display_name": "Hermes",
                    "telegram_username": "project_hermes_bot",
                    "runtime": "hermes",
                    "managed_externally": True,
                    "terminal_enabled": False,
                },
            ],
        }
        document.update(overrides)
        path = self.base / "hub.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_loads_token_by_file_reference_without_embedding_secret(self) -> None:
        path = self.write_config()
        config = load_hub_config(path)
        self.assertEqual(config.require_agent("codex").token_file, self.token.resolve())
        self.assertEqual(
            config.codex_socket_path,
            Path.home() / ".codex/app-server-control/app-server-control.sock",
        )
        self.assertFalse(config.manage_codex_server)
        self.assertNotIn("secret-token-value", path.read_text(encoding="utf-8"))

    def test_rejects_non_boolean_manage_codex_server(self) -> None:
        with self.assertRaisesRegex(HubConfigError, "manage_codex_server"):
            load_hub_config(self.write_config(manage_codex_server="no"))

    def test_rejects_group_chat_id_that_is_not_supergroup_shaped(self) -> None:
        with self.assertRaisesRegex(HubConfigError, "telegram_chat_id"):
            load_hub_config(
                self.write_config(projects=[{"project_id": "pythia", "telegram_chat_id": 123}])
            )

    def test_allows_unbound_group_only_during_bootstrap(self) -> None:
        path = self.write_config(projects=[{"project_id": "pythia", "telegram_chat_id": None}])
        with self.assertRaisesRegex(HubConfigError, "unbound"):
            load_hub_config(path)
        config = load_hub_config(path, allow_unbound=True)
        self.assertIsNone(config.projects[0].telegram_chat_id)

    def test_rejects_world_readable_token_file(self) -> None:
        self.token.chmod(0o644)
        with self.assertRaisesRegex(HubConfigError, "0600"):
            load_hub_config(self.write_config())

    def test_rejects_inline_token_field(self) -> None:
        agents = [
            {
                "agent_id": "codex",
                "display_name": "Codex",
                "telegram_username": "project_codex_bot",
                "runtime": "codex",
                "token": "must-not-be-here",
                "token_file": str(self.token),
            }
        ]
        with self.assertRaisesRegex(HubConfigError, "inline token"):
            load_hub_config(self.write_config(agents=agents))

    def test_state_parent_is_not_required_to_exist_during_config_parse(self) -> None:
        missing = self.base / "private" / "state.db"
        config = load_hub_config(self.write_config(state_path=str(missing)))
        self.assertEqual(config.state_path, missing.resolve())


if __name__ == "__main__":
    unittest.main()
