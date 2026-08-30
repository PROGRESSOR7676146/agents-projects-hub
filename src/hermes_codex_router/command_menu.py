from __future__ import annotations

import json
from typing import Callable

from .hub_config import HubConfig
from .telegram import TelegramBotApi

PUBLIC_COMMANDS: tuple[tuple[str, str], ...] = (
    ("status", "Current provider, model and limits"),
    ("model", "Choose provider, model and effort"),
    ("accounts", "Provider accounts and limits"),
    ("new", "Start a new active session"),
    ("local", "Continue in the native CLI"),
    ("return", "Return to Telegram and publish"),
)

GROUP_COMMANDS: tuple[tuple[str, str], ...] = (("menu", "Open project controls"),)

DIRECT_PROVIDER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("status", "Current provider and model"),
    ("model", "Choose model and effort"),
    ("new", "Start a new provider session"),
)

ApiFactory = Callable[[str], TelegramBotApi]


def _desired(
    commands: tuple[tuple[str, str], ...] = PUBLIC_COMMANDS,
) -> list[dict[str, str]]:
    return [{"command": command, "description": description} for command, description in commands]


def _scope(scope_type: str, *, chat_id: int | None = None) -> str:
    value: dict[str, object] = {"type": scope_type}
    if chat_id is not None:
        value["chat_id"] = chat_id
    return json.dumps(value, separators=(",", ":"))


def configure_public_commands(
    config: HubConfig,
    *,
    sync: bool,
    api_factory: ApiFactory = TelegramBotApi,
) -> dict[str, object]:
    desired = _desired()
    bots: list[dict[str, object]] = []
    for agent in config.agents:
        if (
            agent.agent_id not in {"codex", "opencode", "antigravity"}
            or agent.managed_externally
            or agent.token_file is None
        ):
            continue
        api = api_factory(agent.token_file.read_text(encoding="utf-8").strip())
        direct = desired if agent.agent_id == "codex" else _desired(DIRECT_PROVIDER_COMMANDS)
        scoped: list[tuple[str | None, list[dict[str, str]]]] = [(None, direct)]
        for project in config.projects:
            if project.telegram_chat_id is not None:
                scoped.append(
                    (
                        _scope("chat", chat_id=project.telegram_chat_id),
                        _desired(GROUP_COMMANDS) if agent.agent_id == "codex" else [],
                    )
                )
        matches = True
        changed = False
        for scope, expected in scoped:
            params = {"scope": scope} if scope is not None else {}
            current = api.call("getMyCommands", **params)
            scope_matches = current == expected
            # Telegram reports both an unset scope and an explicitly empty
            # scope as []. Re-assert empty project scopes during every sync so
            # provider defaults cannot leak back into the shared group menu.
            if sync and (not scope_matches or not expected):
                api.call("setMyCommands", commands=json.dumps(expected), **params)
                changed = True
                scope_matches = api.call("getMyCommands", **params) == expected
            matches = matches and scope_matches
        bots.append(
            {
                "agent_id": agent.agent_id,
                "matches": matches,
                "changed": changed,
            }
        )
    return {
        "ok": bool(bots) and all(bool(item["matches"]) for item in bots),
        "sync": sync,
        "commands": [item[0] for item in PUBLIC_COMMANDS],
        "group_commands": [item[0] for item in GROUP_COMMANDS],
        "bots": bots,
    }
