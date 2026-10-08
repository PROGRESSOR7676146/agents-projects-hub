"""Reserved owner controls; journal authority never enters productive routing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from .assessment_inputs import OutcomeAssessmentInput
from .hub_config import HubConfig
from .state import HubState, StateError
from .telegram import TopicMessage


@dataclass(frozen=True, slots=True)
class AssessmentControlDecision:
    created: bool
    text: str | None = None


class ControllerAssessmentOrchestrator:
    def __init__(self, config: HubConfig, state: HubState, ingress_identity: str) -> None:
        self.config, self.state, self.ingress_identity = config, state, ingress_identity

    def preflight(self, message: TopicMessage) -> AssessmentControlDecision | None:
        # A duplicate provider poller must not consume the central ingress receipt.
        if (
            message.chat_id != message.sender_id
            and self.config.hub_bot is not None
            and self.ingress_identity != "hub"
        ):
            return AssessmentControlDecision(False)
        text = None
        if message.sender_id not in self.config.owner_user_ids:
            text = "Only a configured owner can record an outcome."
        elif message.chat_id == message.sender_id:
            text = "Record outcomes by replying to the saved final in its registered project topic."
        elif not (
            self.ingress_identity == "hub"
            and self.config.hub_bot is not None
            and self.config.dispatch_mode == "queue"
            and self.config.queue_runtime == "external"
            and self.config.outbox_runtime == "external"
        ):
            text = "Outcome assessment is unavailable in this mode; nothing was recorded."
        if text is None:
            return None
        created = self.state.claim_message(
            message.chat_id, message.message_id, observer_agent_id=self.ingress_identity
        )
        # Immediate refusals are best-effort controls, with no assessment authority.
        return AssessmentControlDecision(created, text if created else None)

    def record(
        self, message: TopicMessage, project_id: str, *, raw_message: object
    ) -> AssessmentControlDecision:
        # Preserve distinctions lost by quote/material normalization, without
        # storing a raw message, nested replied-to output or update transport ID.
        raw = json.dumps(raw_message, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        material = json.dumps(
            {
                "attachments": [asdict(item) for item in message.attachments],
                "unavailable": message.unavailable_materials,
                "media_group_id": message.media_group_id,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        request = OutcomeAssessmentInput(
            owner_user_id=message.sender_id,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            reply_message_id=message.reply_to_message_id,
            text=message.text,
            text_source=message.text_source,
            is_forwarded=message.is_forwarded,
            quote_text=message.quote_text,
            has_material=bool(
                message.attachments or message.unavailable_materials or message.media_group_id
            ),
            material_fingerprint=hashlib.sha256(material.encode("utf-8")).hexdigest(),
            transport_message_fingerprint=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        )
        try:
            _, created = self.state.record_outcome_assessment(
                request, project_id=project_id, owner_user_ids=self.config.owner_user_ids
            )
        except StateError as error:
            if str(error) not in {"assessment_input_conflict", "assessment_input_already_disposed"}:
                raise
            # Existing durable input controls this ID. Never retry a contradiction
            # or fall through to provider routing; raw content stays private.
            return AssessmentControlDecision(False)
        return AssessmentControlDecision(created)
