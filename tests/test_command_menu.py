from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.command_menu import PUBLIC_COMMANDS, configure_public_commands
from hermes_codex_router.hub_config import AgentDefinition, HubConfig, TerminalSettings


class FakeApi:
    def __init__(self, commands: list[dict[str, str]]) -> None:
        self.commands: dict[str | None, list[dict[str, str]]] = {None: commands}
        self.set_calls = 0

    def call(self, method: str, **params: object) -> object:
        if method == "getMyCommands":
            return self.commands.get(cast(str | None, params.get("scope")), [])
        if method == "setMyCommands":
            import json

            self.commands[cast(str | None, params.get("scope"))] = cast(
                list[dict[str, str]], json.loads(str(params["commands"]))
            )
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
            [item["command"] for item in api.commands[None]],
            [item[0] for item in PUBLIC_COMMANDS],
        )
        self.assertNotIn("new all", str(api.commands))

    def test_only_codex_publishes_universal_commands_in_project_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            tokens = []
            for name in ("codex", "opencode"):
                token = base / name
                token.write_text(f"123:{name}-token", encoding="utf-8")
                token.chmod(0o600)
                tokens.append(token)
            agents = tuple(
                AgentDefinition(
                    name,
                    name.title(),
                    f"project_{name}_bot",
                    name,
                    token,
                    True,
                    False,
                    "provider-selected",
                    "high",
                )
                for name, token in zip(("codex", "opencode"), tokens, strict=True)
            )
            from hermes_codex_router.hub_config import ProjectBinding

            config = HubConfig(
                schema_version=1,
                owner_user_ids=(1,),
                registry_path=base / "projects.json",
                state_path=base / "state.db",
                codex_socket_path=base / "socket",
                manage_codex_server=False,
                terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
                projects=(ProjectBinding("project", -1001234567890),),
                agents=agents,
            )
            apis = {name: FakeApi([]) for name in ("codex", "opencode")}
            configure_public_commands(
                config,
                sync=True,
                api_factory=cast(
                    Any,
                    lambda token: apis["codex"] if "codex" in token else apis["opencode"],
                ),
            )
            chat_scope = '{"type":"chat","chat_id":-1001234567890}'
            self.assertEqual(
                [item["command"] for item in apis["codex"].commands[chat_scope]],
                [item[0] for item in PUBLIC_COMMANDS],
            )
            self.assertEqual(apis["opencode"].commands[chat_scope], [])
            self.assertEqual(
                [item["command"] for item in apis["opencode"].commands[None]],
                ["status", "new"],
            )


if __name__ == "__main__":
    unittest.main()
