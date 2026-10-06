"""Exact plain Reply retry controls, separate from productive Telegram admission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .hub_config import HubConfig
from .models import ProjectRegistry
from .state import HubState, StateError, TopicRecord
from .telegram import TopicMessage
from .topic_execution import ExecutionRootError, resolve_topic_execution_root
from .turn_continuation_state import TurnContinuationState
from .work_retry_state import WorkRetryState


@dataclass(frozen=True, slots=True)
class RetryControlDecision:
    created: bool
    text: str | None = None


class ControllerRetryOrchestrator:
    def __init__(
        self, config: HubConfig, state: HubState, registry: ProjectRegistry, ingress_identity: str
    ) -> None:
        self.config, self.state, self.registry = config, state, registry
        self.ingress_identity = ingress_identity

    def _reject(self, message: TopicMessage, text: str) -> RetryControlDecision:
        created = self.state.claim_message(
            message.chat_id, message.message_id, observer_agent_id=self.ingress_identity
        )
        return RetryControlDecision(created, text if created else None)

    def handle(self, message: TopicMessage, topic: TopicRecord) -> RetryControlDecision | None:
        if (
            message.text.strip().casefold() != "retry"
            or message.reply_to_message_id is None
            or message.is_forwarded
            or message.text_source != "text"
            or message.attachments
            or message.unavailable_materials
            or message.quote_text is not None
        ):
            return None
        if self.state.message_already_observed(message.chat_id, message.message_id):
            return RetryControlDecision(False)
        continuation = TurnContinuationState(self.state)
        source = continuation.source_for_notice(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            notice_message_id=message.reply_to_message_id,
        )
        try:
            root = resolve_topic_execution_root(self.state, self.registry, topic)
            if source is not None:
                _, created, held_count = continuation.continue_from_notice(
                    source_job_id=source,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    notice_message_id=message.reply_to_message_id,
                    reply_message_id=message.message_id,
                    canonical_root=root,
                )
                text = "Continuation accepted in the same Codex session. Codex will first inspect current project state and prior changes."
                if held_count:
                    text += f" {held_count} earlier queued request(s) remain paused for review."
                return RetryControlDecision(created, text if created else None)
            if not (
                self.ingress_identity == "hub"
                and self.config.hub_bot is not None
                and self.config.dispatch_mode == "queue"
                and self.config.queue_runtime == "external"
                and self.config.outbox_runtime == "external"
            ):
                return self._reject(
                    message,
                    "Active-work retry is unsupported in this mode. Inspect /status; no new task was started.",
                )
            reported = WorkRetryState(self.state).report_from_notice(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                notice_message_id=message.reply_to_message_id,
                reply_message_id=message.message_id,
                canonical_root=root,
                now=datetime.now(timezone.utc),
            )
        except (ExecutionRootError, StateError):
            return self._reject(
                message,
                "Retry is paused: the session, root, or writer changed. Inspect /status before trying again.",
            )
        if reported is None:
            return self._reject(
                message,
                "This retry does not identify a delivered task notice in this topic. Reply to that notice or inspect /status; no new task was started.",
            )
        return RetryControlDecision(reported.created)
