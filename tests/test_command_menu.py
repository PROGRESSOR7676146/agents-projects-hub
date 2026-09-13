from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.command_menu import (
    GROUP_COMMANDS,
    PUBLIC_COMMANDS,
    configure_public_commands,
)
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    HubTelegramBot,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.state import HubState


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


def prepare_registry(base: Path, *, include_project: bool) -> None:
    projects: list[dict[str, object]] = []
    if include_project:
        root = base / "project"
        root.mkdir()
        subprocess.run(("git", "init", "-q", str(root)), check=True)
        projects.append(
            {
                "project_id": "project",
                "display_name": "Project",
                "topic_name": "Project",
                "root": str(root),
            }
        )
    (base / "projects.json").write_text(
        json.dumps({"schema_version": 1, "allowed_roots": [str(base)], "projects": projects}),
        encoding="utf-8",
    )


class CommandMenuTests(unittest.TestCase):
    def test_audit_never_creates_or_migrates_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            prepare_registry(base, include_project=False)
            token = base / "hub-token"
            token.write_text("123:hub-token", encoding="utf-8")
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
                agents=(),
                hub_bot=HubTelegramBot("example_hub_bot", token),
            )
            api = FakeApi([])
            result = configure_public_commands(
                config, sync=False, api_factory=cast(Any, lambda _: api)
            )
            self.assertFalse(config.state_path.exists())
            self.assertFalse(result["ok"])

            for version in (26, 27):
                with self.subTest(version=version):
                    connection = sqlite3.connect(config.state_path)
                    connection.execute(f"PRAGMA user_version={version}")
                    connection.commit()
                    connection.close()
                    before = config.state_path.read_bytes()
                    with self.assertRaisesRegex(Exception, "state_schema_unsupported"):
                        configure_public_commands(
                            config, sync=False, api_factory=cast(Any, lambda _: api)
                        )
                    self.assertEqual(config.state_path.read_bytes(), before)
                    self.assertEqual(tuple(base.glob("state.db.backup-*")), ())
                    config.state_path.unlink()

    def test_global_audit_includes_dynamically_onboarded_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            prepare_registry(base, include_project=False)
            dynamic_root = base / "dynamic"
            dynamic_root.mkdir()
            subprocess.run(("git", "init", "-q", str(dynamic_root)), check=True)
            (base / "projects.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "allowed_roots": [str(base)],
                        "projects": [
                            {
                                "project_id": "dynamic",
                                "display_name": "Dynamic",
                                "topic_name": "Dynamic",
                                "root": str(dynamic_root),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            token = base / "hub-token"
            token.write_text("123:hub-token", encoding="utf-8")
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
                agents=(),
                hub_bot=HubTelegramBot("example_hub_bot", token),
            )
            state = HubState.open(config.state_path)
            try:
                now = datetime.now(timezone.utc)
                state._connection.execute(
                    """INSERT INTO project_onboarding_workflows
                       (workflow_id,owner_user_id,display_name,project_id,base_root,canonical_root,
                        stage,expires_at,created_at,updated_at,required_owner_ids_json)
                       VALUES (?,?,?,?,?,?,'completed',?,?,?,'[1]')""",
                    (
                        "dynamic-workflow",
                        1,
                        "Dynamic",
                        "dynamic",
                        str(base),
                        str(dynamic_root),
                        (now + timedelta(hours=1)).isoformat(),
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                state._connection.execute(
                    """INSERT INTO project_group_bindings
                       (project_id,telegram_chat_id,canonical_root,workflow_id,created_at)
                       VALUES ('dynamic',-1002222222222,?,'dynamic-workflow',?)""",
                    (str(dynamic_root), now.isoformat()),
                )
                state._connection.commit()
            finally:
                state.close()
            api = FakeApi([])
            result = configure_public_commands(
                config, sync=True, api_factory=cast(Any, lambda _: api)
            )

        scope = '{"type":"chat","chat_id":-1002222222222}'
        self.assertTrue(result["ok"])
        self.assertEqual(
            [item["command"] for item in api.commands[scope]],
            [item[0] for item in GROUP_COMMANDS],
        )

    def test_syncs_exact_public_menu_and_excludes_new_all(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            prepare_registry(base, include_project=False)
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

    def test_codex_publishes_group_menu_without_configured_hub_bot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            prepare_registry(base, include_project=True)
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
                [item[0] for item in GROUP_COMMANDS],
            )
            self.assertEqual(apis["opencode"].commands[chat_scope], [])
            self.assertEqual(
                [item["command"] for item in apis["opencode"].commands[None]],
                ["status", "model", "new"],
            )

    def test_configured_hub_bot_publishes_group_menu_and_clears_provider_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            prepare_registry(base, include_project=True)
            tokens: dict[str, Path] = {}
            for name in ("hub", "codex", "opencode"):
                token = base / name
                token.write_text(f"123:{name}-token", encoding="utf-8")
                token.chmod(0o600)
                tokens[name] = token
            agents = tuple(
                AgentDefinition(
                    name,
                    name.title(),
                    f"project_{name}_bot",
                    name,
                    tokens[name],
                    True,
                    False,
                    "provider-selected",
                    "high",
                )
                for name in ("codex", "opencode")
            )
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
                hub_bot=HubTelegramBot("project_hub_bot", tokens["hub"]),
            )
            apis = {name: FakeApi([]) for name in ("hub", "codex", "opencode")}
            configure_public_commands(
                config,
                sync=True,
                api_factory=cast(
                    Any,
                    lambda token: next(api for name, api in apis.items() if name in token),
                ),
            )

            chat_scope = '{"type":"chat","chat_id":-1001234567890}'
            self.assertEqual(
                [item["command"] for item in apis["hub"].commands[chat_scope]],
                [item[0] for item in GROUP_COMMANDS],
            )
            self.assertEqual(apis["codex"].commands[chat_scope], [])
            self.assertEqual(apis["opencode"].commands[chat_scope], [])
            self.assertEqual(
                [item["command"] for item in apis["codex"].commands[None]],
                [item[0] for item in PUBLIC_COMMANDS],
            )
            self.assertEqual(
                [item["command"] for item in apis["opencode"].commands[None]],
                ["status", "model", "new"],
            )


if __name__ == "__main__":
    unittest.main()
