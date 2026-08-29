from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    arguments: tuple[str, ...]


COMMAND = re.compile(r"^/([A-Za-z][A-Za-z0-9_]*)(?:@[A-Za-z][A-Za-z0-9_]*)?(?:\s+(.*))?$")


def parse_command(text: str) -> Command | None:
    match = COMMAND.fullmatch(text.strip())
    if not match:
        return None
    raw_arguments = match.group(2) or ""
    arguments = tuple(piece.casefold() for piece in raw_arguments.split())
    return Command(match.group(1).casefold(), arguments)


def decide_targets(
    text: str,
    *,
    active_agent: str,
    usernames: Mapping[str, str],
) -> tuple[str, ...]:
    """Route explicit known mentions, otherwise target only the active agent."""
    positions: list[tuple[int, str]] = []
    for agent_id, username in usernames.items():
        pattern = re.compile(rf"(?<![A-Za-z0-9_])@{re.escape(username)}\b", re.IGNORECASE)
        match = pattern.search(text)
        if match:
            positions.append((match.start(), agent_id))
    if not positions:
        return (active_agent,)
    positions.sort()
    seen: set[str] = set()
    targets: list[str] = []
    for _, agent_id in positions:
        if agent_id not in seen:
            seen.add(agent_id)
            targets.append(agent_id)
    return tuple(targets)
