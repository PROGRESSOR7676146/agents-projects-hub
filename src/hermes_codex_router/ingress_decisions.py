from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, Mapping

from .routing import Command, decide_targets, is_emergency_stop, mentioned_targets, parse_command
from .telegram import TopicMessage

Admission = Literal[
    "accept",
    "reject_material",
    "reject_material_inline",
    "reject_multiple_queue_targets",
    "reject_empty_request",
]

CONTROL_COMMANDS = frozenset(
    {
        "menu",
        "pilot",
        "status",
        "accounts",
        "new",
        "terminal",
        "release",
        "local",
        "return",
        "model",
        "agent",
        "connect",
    }
)


@dataclass(frozen=True, slots=True)
class IngressDecisionContext:
    active_agent_id: str
    pending_batch_agent_id: str | None
    usernames: Mapping[str, str]
    hub_username: str | None
    managed_external_agent_ids: frozenset[str]
    queue_enabled_agent_ids: frozenset[str]
    primary_agent_id: str


@dataclass(frozen=True, slots=True)
class PassiveForwardDecision:
    targets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EmergencyStopDecision:
    admission: Literal["accept"] = "accept"


@dataclass(frozen=True, slots=True)
class ControlCommandDecision:
    command: Command
    admission: Literal["accept", "reject_material"]


@dataclass(frozen=True, slots=True)
class IgnoreDecision:
    reason: Literal["managed_external_only"]
    pending_batch_eligible: bool = False


@dataclass(frozen=True, slots=True)
class ProductiveRouteDecision:
    targets: tuple[str, ...]
    local_targets: tuple[str, ...]
    external_targets: tuple[str, ...]
    prompt_target_id: str
    routing_text: str
    prompt_text: str
    admission: Admission
    requires_inline_root: bool
    pending_batch_eligible: bool
    parsed_command: Command | None = None


IngressDecision = (
    PassiveForwardDecision
    | EmergencyStopDecision
    | ControlCommandDecision
    | IgnoreDecision
    | ProductiveRouteDecision
)


def _remove_mention(text: str, username: str | None) -> str:
    if not username:
        return text.strip()
    return re.sub(
        rf"(?i)(?<![A-Za-z0-9_])@{re.escape(username.removeprefix('@'))}\b",
        "",
        text,
    ).strip()


def _clean_provider_mention(text: str, target: str, usernames: Mapping[str, str]) -> str:
    return _remove_mention(text, usernames.get(target))


def decide_ingress(message: TopicMessage, context: IngressDecisionContext) -> IngressDecision:
    """Classify one Telegram message without consulting state or external services.

    The decision deliberately contains no authorization, project, topic, material,
    session, or durable-admission behavior.  Those remain owned by the Controller.
    ``admission="accept"`` is only a pure routing disposition; it is never proof
    that durable admission has succeeded.
    """
    if message.is_forwarded:
        return PassiveForwardDecision()
    if is_emergency_stop(message.text):
        return EmergencyStopDecision()

    command = parse_command(message.text)
    has_material = bool(message.attachments or message.unavailable_materials)
    if command is not None and (has_material or command.name in CONTROL_COMMANDS):
        admission: Literal["accept", "reject_material"] = (
            "reject_material" if has_material else "accept"
        )
        return ControlCommandDecision(command=command, admission=admission)

    routing_text = _remove_mention(message.text, context.hub_username)
    known_mentions = mentioned_targets(routing_text, usernames=context.usernames)
    pending_batch_eligible = message.reply_to_username is None and not known_mentions
    active_agent_id = context.active_agent_id
    if (
        pending_batch_eligible
        and context.pending_batch_agent_id is not None
        and context.pending_batch_agent_id in context.queue_enabled_agent_ids
    ):
        active_agent_id = context.pending_batch_agent_id

    targets = decide_targets(
        routing_text,
        active_agent=active_agent_id,
        usernames=context.usernames,
        reply_to_username=message.reply_to_username,
    )
    local_targets = tuple(
        target for target in targets if target not in context.managed_external_agent_ids
    )
    external_targets = tuple(
        target for target in targets if target in context.managed_external_agent_ids
    )
    if not local_targets:
        return IgnoreDecision(
            reason="managed_external_only",
            pending_batch_eligible=pending_batch_eligible,
        )

    requires_inline_root = any(
        target not in context.queue_enabled_agent_ids for target in local_targets
    )
    first_local_target = next(iter(local_targets))
    prompt_target_id = (
        context.primary_agent_id
        if context.primary_agent_id in local_targets
        else first_local_target
    )
    if has_material and requires_inline_root:
        route_admission: Admission = "reject_material_inline"
    elif context.primary_agent_id in context.queue_enabled_agent_ids and len(local_targets) > 1:
        route_admission = "reject_multiple_queue_targets"
    elif (
        not _clean_provider_mention(routing_text, prompt_target_id, context.usernames)
        and not has_material
    ):
        route_admission = "reject_empty_request"
    else:
        route_admission = "accept"

    prompt_text = _clean_provider_mention(routing_text, prompt_target_id, context.usernames)
    if not prompt_text and has_material:
        prompt_text = "Review the attached Telegram material."

    return ProductiveRouteDecision(
        targets=targets,
        local_targets=local_targets,
        external_targets=external_targets,
        prompt_target_id=prompt_target_id,
        routing_text=routing_text,
        prompt_text=prompt_text,
        admission=route_admission,
        requires_inline_root=requires_inline_root,
        pending_batch_eligible=pending_batch_eligible,
        parsed_command=command,
    )
