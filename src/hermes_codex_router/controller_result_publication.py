from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .artifacts import (
    ValidatedArtifact,
    artifact_spool_root,
    cleanup_job_staging,
    remove_spooled_artifact,
)
from .incoming_materials import (
    PreparedIncomingMaterials,
    cleanup_consumed_raw_inputs,
    cleanup_materialized_inputs,
)
from .state import HubState, ProviderJobRecord, ProviderJobResultRecord, StateError


@dataclass(frozen=True, slots=True)
class PreparedResultPublication:
    job: ProviderJobRecord
    project_root: Path
    prepared_materials: PreparedIncomingMaterials
    visible_response: str
    telegram_html: str
    provider_session_id: str | None
    actual_model: str | None
    telegram_contract_version: int
    artifacts: tuple[ValidatedArtifact, ...] = ()


@dataclass(frozen=True, slots=True)
class PublishedProviderResult:
    result: ProviderJobResultRecord
    artifacts: tuple[ValidatedArtifact, ...]


class PreparedResultPublisher:
    """Publish one prepared result without owning state or Telegram delivery."""

    def __init__(
        self,
        *,
        state: HubState,
        state_path: Path,
        cleanup_error: Callable[[str, str], None] | None = None,
    ) -> None:
        self.state = state
        self.state_path = state_path
        self.cleanup_error = cleanup_error

    def publish(self, publication: PreparedResultPublication) -> PublishedProviderResult:
        token = publication.job.lease_token
        if token is None:
            raise StateError("prepared result publication requires an active lease")

        try:
            result = self.state.commit_provider_result(
                publication.job.job_id,
                token,
                visible_response=publication.visible_response,
                sender_agent_id=publication.job.agent_id,
                telegram_html=publication.telegram_html,
                provider_session_id=publication.provider_session_id,
                actual_model=publication.actual_model,
                user_excerpt=publication.job.payload_text,
                acknowledge_context=publication.job.context_watermark is not None,
                acknowledge_handoff=publication.job.handoff_id is not None,
                telegram_contract_version=publication.telegram_contract_version,
                artifacts=publication.artifacts,
            )
        except BaseException:
            spool_root = artifact_spool_root(self.state_path)
            for artifact in publication.artifacts:
                try:
                    remove_spooled_artifact(artifact.path, spool_root)
                except Exception:
                    pass
            raise
        cleanup_consumed_raw_inputs(publication.prepared_materials)
        cleanup_materialized_inputs(publication.prepared_materials)
        try:
            cleanup_job_staging(publication.project_root, publication.job.job_id)
        except Exception as exc:
            if self.cleanup_error is not None:
                try:
                    self.cleanup_error("artifact_staging_cleanup_error", type(exc).__name__)
                except Exception:
                    pass
        return PublishedProviderResult(result=result, artifacts=publication.artifacts)
