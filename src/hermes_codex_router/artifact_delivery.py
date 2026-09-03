from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Protocol

from .artifacts import (
    artifact_spool_root,
    cleanup_job_staging,
    remove_spooled_artifact,
    spool_staged_artifacts,
    verify_spooled_artifact,
)


class ImmediateDocumentSender(Protocol):
    def send_html(self, chat_id: int, thread_id: int, html: str) -> int: ...

    def send_document(
        self,
        chat_id: int,
        thread_id: int,
        document_path: Path,
        *,
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> int: ...


def deliver_staged_artifacts_immediately(
    telegram: ImmediateDocumentSender,
    *,
    chat_id: int,
    thread_id: int,
    project_root: Path,
    state_path: Path,
    job_id: str,
) -> tuple[str, ...]:
    """Securely snapshot and deliver artifacts for legacy/DM inline turns.

    Queue workers use the durable Telegram outbox. This compatibility path keeps
    the same staging validation and immutable spool boundary, but delivery is
    immediate because legacy direct-message state has no provider-job outbox.
    """
    rejected: list[str] = []
    spool_root = artifact_spool_root(state_path)
    artifacts = spool_staged_artifacts(project_root, job_id, spool_root, rejection_sink=rejected)
    cleanup_job_staging(project_root, job_id)
    for artifact in artifacts:
        verify_spooled_artifact(
            artifact.path,
            spool_root,
            expected_size=artifact.size,
            expected_sha256=artifact.sha256,
        )
        telegram.send_document(
            chat_id,
            thread_id,
            artifact.path,
            file_name=artifact.name,
            mime_type=artifact.mime_type,
        )
        remove_spooled_artifact(artifact.path, spool_root)
    for detail in rejected:
        telegram.send_html(chat_id, thread_id, html.escape(f"⚠️ Not attached: {detail}"))
    return tuple(rejected)
