from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    arguments: tuple[str, ...]


COMMAND = re.compile(r"^/([A-Za-z][A-Za-z0-9_]*)(?:@[A-Za-z][A-Za-z0-9_]*)?(?:\s+(.*))?$")
EMERGENCY_STOP = frozenset({"stop", "halt", "стоп", "стой", "остановись", "прекрати"})
CONTEXT_REQUEST = re.compile(
    r"^/context(?:@[A-Za-z][A-Za-z0-9_]*)?(?:\s+([A-Za-z0-9_-]+))?"
    r"(?:\s+([1-9]|1[0-9]|20))?$",
    re.IGNORECASE,
)


def is_emergency_stop(text: str) -> bool:
    """Match only a whole emergency utterance; never scan normal prose."""
    normalized = " ".join(text.strip().casefold().split()).rstrip(".!！。")
    if normalized.startswith("/"):
        normalized = normalized[1:].split("@", 1)[0]
    return normalized in EMERGENCY_STOP


def parse_command(text: str) -> Command | None:
    match = COMMAND.fullmatch(text.strip())
    if not match:
        return None
    raw_arguments = match.group(2) or ""
    arguments = tuple(piece.casefold() for piece in raw_arguments.split())
    return Command(match.group(1).casefold(), arguments)


def parse_context_request(text: str) -> tuple[str | None, int] | None:
    """Parse the advanced explicit-history command without advertising it in menus."""
    match = CONTEXT_REQUEST.fullmatch(text.strip())
    if match is None:
        return None
    source = match.group(1)
    return (source.casefold() if source else None, int(match.group(2) or "8"))


def decide_targets(
    text: str,
    *,
    active_agent: str,
    usernames: Mapping[str, str],
    reply_to_username: str | None = None,
) -> tuple[str, ...]:
    """Route a real Telegram reply, then mentions, then the active agent."""
    if reply_to_username is not None:
        replied = reply_to_username.removeprefix("@").casefold()
        for agent_id, username in usernames.items():
            if username.removeprefix("@").casefold() == replied:
                return (agent_id,)
    mentioned = mentioned_targets(text, usernames=usernames)
    return mentioned or (active_agent,)


def mentioned_targets(text: str, *, usernames: Mapping[str, str]) -> tuple[str, ...]:
    """Return only explicit provider mentions, preserving their text order."""
    positions: list[tuple[int, str]] = []
    for agent_id, username in usernames.items():
        pattern = re.compile(rf"(?<![A-Za-z0-9_])@{re.escape(username)}\b", re.IGNORECASE)
        match = pattern.search(text)
        if match:
            positions.append((match.start(), agent_id))
    positions.sort()
    seen: set[str] = set()
    targets: list[str] = []
    for _, agent_id in positions:
        if agent_id not in seen:
            seen.add(agent_id)
            targets.append(agent_id)
    return tuple(targets)
