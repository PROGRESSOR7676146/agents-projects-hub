"""Validation of the optional ``agent_mail`` Hub configuration (ADR 0112).

Mail is off unless the configuration lists at least one directed route between
two numeric topics of project groups. A route names its endpoints
by project ID plus numeric chat and thread IDs; titles never select a route.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .agent_mail import ALIAS_PATTERN, MailRoute

MAX_ROUTES = 16
_ROUTE_KEYS = frozenset({"alias", "from", "to"})
_ENDPOINT_KEYS = frozenset({"project_id", "chat_id", "thread_id"})


class AgentMailConfigError(ValueError):
    pass


def parse_agent_mail(
    value: object,
    *,
    project_chats: Mapping[str, int | None],
    supported: bool,
) -> tuple[MailRoute, ...]:
    """Return configured routes; an absent key or empty list keeps mail off."""
    if value is None:
        return ()
    if not isinstance(value, dict) or set(value) - {"routes"}:
        raise AgentMailConfigError("agent_mail must be an object with only routes")
    raw_routes = value.get("routes", [])
    if not isinstance(raw_routes, list) or len(raw_routes) > MAX_ROUTES:
        raise AgentMailConfigError(f"agent_mail.routes must be a list of at most {MAX_ROUTES}")
    if raw_routes and not supported:
        raise AgentMailConfigError(
            "agent_mail requires the Hub bot and the external queue and outbox"
        )
    routes: list[MailRoute] = []
    seen: set[tuple[int, int, str]] = set()
    for raw in raw_routes:
        if not isinstance(raw, dict) or set(raw) != _ROUTE_KEYS:
            raise AgentMailConfigError("each agent_mail route needs exactly alias, from and to")
        alias = raw["alias"]
        if not isinstance(alias, str) or ALIAS_PATTERN.match(alias) is None:
            raise AgentMailConfigError("agent_mail route alias must match [a-z][a-z0-9-]{0,15}")
        source = _endpoint(raw["from"], project_chats)
        target = _endpoint(raw["to"], project_chats)
        if source[1:] == target[1:]:
            raise AgentMailConfigError("an agent_mail route must connect two different topics")
        key = (source[1], source[2], alias)
        if key in seen:
            raise AgentMailConfigError("agent_mail route aliases must be unique per source topic")
        seen.add(key)
        routes.append(
            MailRoute(
                alias=alias,
                source_project_id=source[0],
                source_chat_id=source[1],
                source_thread_id=source[2],
                target_project_id=target[0],
                target_chat_id=target[1],
                target_thread_id=target[2],
            )
        )
    return tuple(routes)


def _endpoint(raw: Any, project_chats: Mapping[str, int | None]) -> tuple[str, int, int]:
    if not isinstance(raw, dict) or set(raw) != _ENDPOINT_KEYS:
        raise AgentMailConfigError("an agent_mail endpoint needs project_id, chat_id and thread_id")
    project_id, chat_id, thread_id = raw["project_id"], raw["chat_id"], raw["thread_id"]
    if (
        not isinstance(project_id, str)
        or type(chat_id) is not int
        or type(thread_id) is not int
        or thread_id <= 0
    ):
        raise AgentMailConfigError("an agent_mail endpoint has an invalid value")
    # A statically configured project must use its own group. A project bound
    # by onboarding is checked at runtime against its current group binding,
    # as every display, notice and press is; no group may serve two projects.
    if project_id in project_chats:
        if project_chats[project_id] != chat_id:
            raise AgentMailConfigError(
                "an agent_mail endpoint must use the configured group of its project"
            )
    elif chat_id in project_chats.values():
        raise AgentMailConfigError("an agent_mail endpoint group belongs to another project")
    return project_id, chat_id, thread_id
