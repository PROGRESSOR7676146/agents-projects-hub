from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .artifacts import ValidatedArtifact
from .incoming_materials import PreparedIncomingMaterials, cleanup_consumed_raw_inputs
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

    def __init__(self, *, state: HubState, state_path: Path) -> None:
        self.state = state
        self.state_path = state_path

    def publish(self, publication: PreparedResultPublication) -> PublishedProviderResult:
        token = publication.job.lease_token
        if token is None:
            raise StateError("prepared result publication requires an active lease")

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
        cleanup_consumed_raw_inputs(publication.prepared_materials)
        return PublishedProviderResult(result=result, artifacts=publication.artifacts)
