from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.command_menu import PUBLIC_COMMANDS, configure_public_commands
from hermes_codex_router.hub_config import AgentDefinition, HubConfig, TerminalSettings


class FakeApi:
    def __init__(self, commands: list[dict[str, str]]) -> None:
        self.commands = commands
        self.set_calls = 0

    def call(self, method: str, **params: object) -> object:
        if method == "getMyCommands":
            return self.commands
        if method == "setMyCommands":
            import json

            self.commands = cast(list[dict[str, str]], json.loads(str(params["commands"])))
            self.set_calls += 1
            return True
        raise AssertionError(method)


class CommandMenuTests(unittest.TestCase):
    def test_syncs_exact_public_menu_and_excludes_new_all(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            token = base / "token"
            token.write_text("123:token", encoding="utf-8")
            token.chmod(0o600)
            config = HubConfig(
                schema_version=1,
                owner_user_ids=(1,),
                registry_path=base / "projects.json",
                state_path=base / "state.db",
                codex_socket_path=base / "socket",
                manage_codex_server=False,
                terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
                projects=(),
                agents=(
                    AgentDefinition(
                        "codex",
                        "Codex",
                        "codex_test_bot",
                        "codex",
                        token,
                        True,
                        False,
                        "gpt",
                        "high",
                    ),
                ),
            )
            api = FakeApi([{"command": "agent", "description": "legacy"}])
            result = configure_public_commands(
                config,
                sync=True,
                api_factory=cast(Any, lambda _: api),
            )
        self.assertTrue(result["ok"])
        self.assertEqual(api.set_calls, 1)
        self.assertEqual(
            [item["command"] for item in api.commands],
            [item[0] for item in PUBLIC_COMMANDS],
        )
        self.assertNotIn("new all", str(api.commands))


if __name__ == "__main__":
    unittest.main()
