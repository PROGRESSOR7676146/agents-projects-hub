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

ApiFactory = Callable[[str], TelegramBotApi]


def _desired() -> list[dict[str, str]]:
    return [
        {"command": command, "description": description} for command, description in PUBLIC_COMMANDS
    ]


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
        current = api.call("getMyCommands")
        matches = current == desired
        changed = False
        if sync and not matches:
            api.call("setMyCommands", commands=json.dumps(desired))
            changed = True
            matches = api.call("getMyCommands") == desired
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
        "bots": bots,
    }
