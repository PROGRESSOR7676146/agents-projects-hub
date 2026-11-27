from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from .incoming_materials import (
    ALBUM_DOWNLOAD_HOLD_MILLISECONDS,
    ALBUM_MAX_MILLISECONDS,
    ALBUM_QUIET_MILLISECONDS,
    IncomingDownloadTransport,
    IncomingMaterialDraft,
    receive_incoming_materials,
)
from .provider_queue_capacity import QueueCapacityConfig
from .state import (
    HubState,
    ProviderJobRecord,
    SessionRecord,
    StateError,
    TopicRecord,
    WriterTransferSnapshot,
)
from .state_errors import CodexPermissionSelectionChanged
from .state_handoff import HandoffAdmission
from .telegram import TelegramError, TopicMessage
from .telegram_quoted_context import bounded_payload, with_quoted_context
from .telegram_turn_provenance import validate_ingress_identity

AdmissionRejection = Literal[
    "input_too_long",
    "input_before_session_activation",
    "local_transfer_changed",
    "persistent_root_blocker",
    "codex_permission_selection_changed",
    "session_changed",
]
AdmissionFailureReason = Literal["material_download", "enqueue_uncommitted"]


@dataclass(frozen=True, slots=True)
class DurableAdmissionRequest:
    message: TopicMessage
    topic: TopicRecord
    session: SessionRecord
    prompt: str
    context_watermark: int | None = None
    handoff_id: str | None = None
    batchable_user_text: str | None = None
    take_local_writer: bool = False
    writer_transfer_snapshot: WriterTransferSnapshot | None = None
    session_handoff: HandoffAdmission | None = None


@dataclass(frozen=True, slots=True)
class CommittedAdmission:
    job: ProviderJobRecord


@dataclass(frozen=True, slots=True)
class DuplicateAdmission:
    pass


@dataclass(frozen=True, slots=True)
class RejectedAdmission:
    reason: AdmissionRejection


@dataclass(frozen=True, slots=True)
class DurableAdmissionFailure:
    reason: AdmissionFailureReason
    error: Exception = field(repr=False)


DurableAdmissionResult = (
    CommittedAdmission | DuplicateAdmission | RejectedAdmission | DurableAdmissionFailure
)
WriterTransferPreflight = Callable[[], WriterTransferSnapshot]


class DurableProviderAdmission:
    def __init__(
        self,
        *,
        state: HubState,
        telegram: IncomingDownloadTransport,
        state_path: Path,
        observer_agent_id: str,
        message_batch_quiet_ms: int,
        message_batch_max_ms: int,
        prepare_task_notices: bool = False,
        queue_capacity: QueueCapacityConfig | None = None,
        telegram_ingress_identity: str | None = None,
    ) -> None:
        self.state = state
        self.telegram = telegram
        self.state_path = state_path
        self.observer_agent_id = observer_agent_id
        self.message_batch_quiet_ms = message_batch_quiet_ms
        self.message_batch_max_ms = message_batch_max_ms
        self.prepare_task_notices = prepare_task_notices
        self.queue_capacity = queue_capacity
        self.telegram_ingress_identity = validate_ingress_identity(telegram_ingress_identity)

    def admit(
        self,
        request: DurableAdmissionRequest,
        *,
        writer_transfer_preflight: WriterTransferPreflight | None = None,
    ) -> DurableAdmissionResult:
        message = request.message
        handoff = request.session_handoff
        model_handoff = (
            handoff is not None and handoff.source_session_id != request.session.session_id
        )
        if self.state.message_already_observed(message.chat_id, message.message_id):
            return DuplicateAdmission()

        if handoff is not None and (
            request.batchable_user_text is not None
            or message.attachments
            or message.unavailable_materials
            or request.take_local_writer
        ):
            raise ValueError("session rotation admits one plain text input")
        # A rotated input runs in a new generation with the current selection.
        if request.session.agent_id == "codex" and handoff is None:
            refusal = self._permission_refusal(request)
            if refusal is not None:
                return refusal

        if request.batchable_user_text is not None and len(request.batchable_user_text) > 18_000:
            if not self.state.claim_message(
                message.chat_id,
                message.message_id,
                observer_agent_id=self.observer_agent_id,
            ):
                return DuplicateAdmission()
            return RejectedAdmission("input_too_long")

        if not request.take_local_writer and not model_handoff:
            try:
                blocked = self.state.reject_blocked_provider_input(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    topic_id=request.topic.topic_id,
                    session_id=request.session.session_id,
                    session_generation=request.session.generation,
                )
            except StateError:
                if handoff is not None:
                    return RejectedAdmission("session_changed")
                raise
            if blocked is not None:
                return RejectedAdmission("persistent_root_blocker")

        payload = bounded_payload(
            request.prompt,
            message,
            current_text_length=len(request.batchable_user_text or ""),
            limit=20_000,
        )

        group_key = self._album_group_key(message)
        materials: tuple[IncomingMaterialDraft, ...] = ()
        if message.attachments or message.unavailable_materials:
            if group_key is not None and not request.take_local_writer:
                self.state.hold_queued_input_group(
                    topic_id=request.topic.topic_id,
                    agent_id=request.session.agent_id,
                    session_id=request.session.session_id,
                    session_generation=request.session.generation,
                    input_group_key=group_key,
                    hold_ms=ALBUM_DOWNLOAD_HOLD_MILLISECONDS,
                    max_ms=ALBUM_MAX_MILLISECONDS,
                )
            try:
                materials = receive_incoming_materials(
                    message,
                    telegram=self.telegram,
                    state_path=self.state_path,
                )
            except TelegramError as exc:
                return DurableAdmissionFailure("material_download", exc)

        expected_transfer = request.writer_transfer_snapshot
        if request.take_local_writer and expected_transfer is None:
            if writer_transfer_preflight is not None:
                # This callback deliberately sits after material collection. The
                # Controller owns root validation and therefore owns its errors.
                expected_transfer = writer_transfer_preflight()

        try:
            if (
                request.batchable_user_text is not None or materials
            ) and not request.take_local_writer:
                appended_text = with_quoted_context(
                    request.batchable_user_text or "Review the attached Telegram material.",
                    message,
                    limit=20_000,
                )
                job, created = self.state.enqueue_or_append_provider_job(
                    idempotency_key=f"telegram:{message.chat_id}:{message.message_id}",
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    topic_id=request.topic.topic_id,
                    agent_id=request.session.agent_id,
                    session_id=request.session.session_id,
                    session_generation=request.session.generation,
                    provider_session_id=request.session.provider_session_id,
                    model=request.session.model,
                    effort=request.session.effort,
                    payload_text=payload,
                    context_watermark=request.context_watermark,
                    handoff_id=request.handoff_id,
                    appended_user_text=appended_text,
                    materials=materials,
                    input_group_key=group_key,
                    prepare_task_notices=self.prepare_task_notices,
                    queue_capacity=self.queue_capacity,
                    telegram_ingress_identity=self.telegram_ingress_identity,
                    quiet_ms=(
                        ALBUM_QUIET_MILLISECONDS
                        if group_key is not None
                        else self.message_batch_quiet_ms
                    ),
                    max_ms=(
                        ALBUM_MAX_MILLISECONDS
                        if group_key is not None
                        else self.message_batch_max_ms
                    ),
                )
            else:
                job, created = self.state.enqueue_provider_job(
                    idempotency_key=f"telegram:{message.chat_id}:{message.message_id}",
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    topic_id=request.topic.topic_id,
                    agent_id=request.session.agent_id,
                    session_id=request.session.session_id,
                    session_generation=request.session.generation,
                    provider_session_id=None if handoff else request.session.provider_session_id,
                    model=request.session.model,
                    effort=request.session.effort,
                    payload_text=payload,
                    context_watermark=request.context_watermark,
                    handoff_id=request.handoff_id,
                    materials=materials,
                    input_group_key=group_key,
                    take_local_writer=request.take_local_writer,
                    expected_transfer=expected_transfer,
                    session_handoff=handoff,
                    prepare_task_notices=self.prepare_task_notices,
                    queue_capacity=self.queue_capacity,
                    telegram_ingress_identity=self.telegram_ingress_identity,
                )
        except CodexPermissionSelectionChanged as exc:
            if model_handoff:
                return RejectedAdmission("codex_permission_selection_changed")
            return self._permission_refusal(request) or DurableAdmissionFailure(
                "enqueue_uncommitted", exc
            )
        except StateError as exc:
            if str(exc) == "execution root has a persistent local writer or uncertainty":
                if model_handoff:
                    return RejectedAdmission("persistent_root_blocker")
                blocked = self.state.reject_blocked_provider_input(
                    chat_id=message.chat_id,
                    message_id=message.message_id,
                    topic_id=request.topic.topic_id,
                    session_id=request.session.session_id,
                    session_generation=request.session.generation,
                )
                if blocked is not None:
                    return RejectedAdmission("persistent_root_blocker")
                # The blocker disappeared between two SQLite transactions.
                # Leave the update unclaimed so ingress retries normal admission.
                return DurableAdmissionFailure("enqueue_uncommitted", exc)
            if str(exc) == "input_before_session_activation":
                self.state.claim_message(
                    message.chat_id,
                    message.message_id,
                    observer_agent_id=self.observer_agent_id,
                )
                return RejectedAdmission("input_before_session_activation")
            if request.take_local_writer:
                return RejectedAdmission("local_transfer_changed")
            if handoff is not None:
                # The session or its guards changed: nothing committed or claimed.
                return RejectedAdmission("session_changed")
            return DurableAdmissionFailure("enqueue_uncommitted", exc)
        except Exception as exc:
            return DurableAdmissionFailure("enqueue_uncommitted", exc)

        if not created:
            return DuplicateAdmission()
        return CommittedAdmission(job)

    def _permission_refusal(
        self,
        request: DurableAdmissionRequest,
    ) -> DuplicateAdmission | RejectedAdmission | DurableAdmissionFailure | None:
        try:
            refused = self.state.reject_changed_codex_input(
                chat_id=request.message.chat_id,
                message_id=request.message.message_id,
                thread_id=request.message.thread_id,
                topic_id=request.topic.topic_id,
                session_id=request.session.session_id,
                session_generation=request.session.generation,
            )
        except Exception as exc:
            # A changed snapshot or failed refusal transaction has no durable
            # disposition. Ingress must retain the update for safe redelivery.
            return DurableAdmissionFailure("enqueue_uncommitted", exc)
        if refused is None:
            return None
        return (
            DuplicateAdmission()
            if refused == "duplicate"
            else RejectedAdmission("codex_permission_selection_changed")
        )

    @staticmethod
    def _album_group_key(message: TopicMessage) -> str | None:
        if message.media_group_id is None:
            return None
        raw_group = f"{message.chat_id}:{message.thread_id}:{message.media_group_id}".encode(
            "utf-8"
        )
        return "telegram-album:" + hashlib.sha256(raw_group).hexdigest()


def controller_provider_admission(
    service: Any, message: TopicMessage, session: SessionRecord
) -> DurableProviderAdmission:
    """The Controller's durable admission for one Telegram input (ingress and buttons)."""
    config = service.config
    external = session.agent_id in config.external_worker_agent_ids
    return DurableProviderAdmission(
        state=service.state,
        telegram=service.telegram,
        state_path=config.state_path,
        observer_agent_id=service.agent.agent_id,
        telegram_ingress_identity=service._telegram_group_ingress(message),
        message_batch_quiet_ms=config.message_batch_quiet_ms,
        message_batch_max_ms=config.message_batch_max_ms,
        queue_capacity=QueueCapacityConfig(
            config.max_parallel_roots,
            config.external_worker_agent_ids if external else (),
            {"codex": config.codex_worker_count, "claude": config.claude_worker_count}
            if external
            else {},
        ),
        prepare_task_notices=(
            config.hub_bot is not None
            and config.dispatch_mode == "queue"
            and config.queue_runtime == "external"
            and config.outbox_runtime == "external"
        ),
    )
