"""Backward-compatible Codex name for the provider-scoped queue worker."""

from .external_runtime import ExternalCliAdapter
from .external_worker import ExternalQueueWorker, ExternalQueueWorkerError
from .hub_config import HubConfig
from .registry import ProjectRegistry
from .supervisor import CodexAppServerSupervisor


class CodexWorkerError(ExternalQueueWorkerError):
    pass


class CodexQueueWorker(ExternalQueueWorker):
    """Compatibility wrapper for existing Codex worker deployments and tests."""

    def __init__(
        self,
        config: HubConfig,
        *,
        registry: ProjectRegistry | None = None,
        supervisor: CodexAppServerSupervisor | None = None,
        adapter: ExternalCliAdapter | None = None,
        worker_id: str | None = None,
    ) -> None:
        try:
            super().__init__(
                config,
                "codex",
                registry=registry,
                supervisor=supervisor,
                adapter=adapter,
                worker_id=worker_id,
            )
        except ExternalQueueWorkerError as exc:
            raise CodexWorkerError(str(exc)) from exc
