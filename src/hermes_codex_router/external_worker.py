from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone

from .codex_appserver import CodexAppServerClient, RateLimits
from .external_runtime import ExternalCliAdapter, ExternalRuntimeError, ProviderLimitError
from .hub_config import HubConfig
from .metadata import format_agent_response, format_telegram_response
from .registry import ProjectRegistry, load_registry
from .state import HubState, ProviderJobRecord
from .supervisor import CodexAppServerSupervisor


class ExternalQueueWorkerError(RuntimeError):
    pass


class ExternalQueueWorker:
    """One provider-scoped queue worker with no Telegram transport capability."""

    _LOCAL_RUNTIMES = frozenset({"codex", "opencode", "antigravity"})

    def __init__(
        self,
        config: HubConfig,
        agent_id: str = "codex",
        *,
        registry: ProjectRegistry | None = None,
        supervisor: CodexAppServerSupervisor | None = None,
        adapter: ExternalCliAdapter | None = None,
        worker_id: str | None = None,
    ) -> None:
        if config.dispatch_mode != "queue" or config.queue_runtime != "external":
            raise ExternalQueueWorkerError(
                "external worker requires queue dispatch with external runtime"
            )
        self.config = config
        try:
            self.agent = config.require_agent(agent_id)
        except KeyError as exc:
            raise ExternalQueueWorkerError(f"unknown external worker agent_id: {agent_id}") from exc
        configured_workers = config.external_worker_agent_ids or ("codex",)
        if self.agent.agent_id not in configured_workers:
            raise ExternalQueueWorkerError(
                f"agent {agent_id} is not configured for an external worker"
            )
        if self.agent.runtime not in self._LOCAL_RUNTIMES:
            raise ExternalQueueWorkerError(
                "external worker supports codex, opencode, and antigravity"
            )
        if self.agent.managed_externally:
            raise ExternalQueueWorkerError("external worker agent must be locally managed")
        self.registry = registry or load_registry(config.registry_path)
        self.state = HubState.open(config.state_path)
        self.worker_id = worker_id or f"{self.agent.agent_id}-worker"
        self._started_at = datetime.now(timezone.utc)
        self._process_start_marker = uuid.uuid4().hex
        self._last_success_at: datetime | None = None
        self._last_error_code: str | None = None
        self._provider_state = "unknown"
        self._quota_remaining_percent: float | None = None
        self._quota_reset_at: datetime | None = None
        self.supervisor: CodexAppServerSupervisor | None = None
        self.adapter: ExternalCliAdapter | None = None
        self._codex_client: CodexAppServerClient | None = None
        if self.agent.runtime == "codex":
            self.supervisor = supervisor or CodexAppServerSupervisor(
                config.codex_socket_path,
                manage_process=config.manage_codex_server,
                stdio_executable=config.codex_stdio_executable,
            )
        else:
            self.adapter = adapter or ExternalCliAdapter(
                self.agent.runtime,
                executable=self.agent.executable,
                runtime_home=self.agent.runtime_home,
            )
        self._stop = threading.Event()
        self._publish_health()

    def close(self) -> None:
        self.stop()
        self._discard_client()
        if self.supervisor is not None:
            self.supervisor.stop()
        self.state.close()

    def stop(self) -> None:
        self._stop.set()

    def _client(self) -> CodexAppServerClient:
        if self.supervisor is None:
            raise ExternalQueueWorkerError("Codex client requested for a non-Codex worker")
        if self._codex_client is None:
            self._codex_client = self.supervisor.client()
        return self._codex_client

    def _discard_client(self) -> None:
        client = self._codex_client
        self._codex_client = None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _record_event(self, level: str, code: str, detail: str) -> None:
        try:
            event_state = HubState.open(self.config.state_path)
            try:
                event_state.record_runtime_event(self.agent.agent_id, level, code, detail)
            finally:
                event_state.close()
        except Exception:
            pass

    def _publish_health(
        self,
        *,
        state: HubState | None = None,
        activity_state: str = "idle",
        active_job: ProviderJobRecord | None = None,
    ) -> None:
        """Best-effort cached liveness; health reporting never stops useful work."""
        target = state or self.state
        try:
            target.upsert_runtime_health(
                component="provider_worker",
                instance_id=self.worker_id,
                runtime=self.agent.runtime,
                agent_id=self.agent.agent_id,
                pid=os.getpid(),
                process_start_marker=self._process_start_marker,
                started_at=self._started_at,
                heartbeat_at=datetime.now(timezone.utc),
                success_at=self._last_success_at,
                error_code=self._last_error_code,
                activity_state=activity_state,
                active_job_id=None if active_job is None else active_job.job_id,
                active_lease_expires_at=(
                    None
                    if active_job is None or active_job.lease_expires_at is None
                    else datetime.fromisoformat(active_job.lease_expires_at)
                ),
                provider_state=self._provider_state,
                quota_remaining_percent=self._quota_remaining_percent,
                quota_reset_at=self._quota_reset_at,
            )
        except Exception:
            pass

    def run_forever(self, *, poll_seconds: float = 0.2) -> None:
        if poll_seconds <= 0:
            raise ExternalQueueWorkerError("poll_seconds must be positive")
        if self.supervisor is not None:
            self.supervisor.start()
        try:
            while not self._stop.is_set():
                try:
                    worked = self.run_cycle()
                except Exception as exc:
                    self._record_event("error", "worker_cycle_error", type(exc).__name__)
                    worked = False
                self._stop.wait(0.01 if worked else poll_seconds)
        except KeyboardInterrupt:
            return

    def run_cycle(self) -> bool:
        """Lease and execute at most one job for this worker's sole agent."""
        if self._stop.is_set():
            return False
        self._publish_health()
        self.state.recover_stale_provider_jobs(agent_id=self.agent.agent_id)
        if self._stop.is_set():
            return False
        job = self.state.lease_provider_job(self.agent.agent_id, self.worker_id)
        if job is None:
            return False
        if self._stop.is_set():
            assert job.lease_token is not None
            self.state.release_provider_job_lease(job.job_id, job.lease_token)
            return False
        self._publish_health(activity_state="leased", active_job=job)
        self._execute(job)
        completed = self.state.get_provider_job(job.job_id)
        if completed.status == "result_ready":
            self._last_success_at = datetime.now(timezone.utc)
            self._last_error_code = None
            self._provider_state = "ready"
            self._quota_remaining_percent = None
            self._quota_reset_at = None
        self._publish_health()
        return True

    def _execute(self, job: ProviderJobRecord) -> None:
        if job.lease_token is None:
            raise ExternalQueueWorkerError("leased provider job has no lease token")
        executing = self.state.mark_provider_job_executing(job.job_id, job.lease_token)
        self._publish_health(activity_state="executing", active_job=executing)
        token = executing.lease_token
        assert token is not None
        topic = self.state.get_topic(executing.topic_id)
        project = self.registry.require_project(topic.project_id)
        heartbeat_stop = threading.Event()

        def maintain_lease() -> None:
            heartbeat_state = HubState.open(self.config.state_path)
            try:
                while not heartbeat_stop.is_set():
                    try:
                        heartbeat_state.heartbeat_provider_job(
                            executing.job_id, token, lease_seconds=120
                        )
                        refreshed = heartbeat_state.get_provider_job(executing.job_id)
                        self._publish_health(
                            state=heartbeat_state,
                            activity_state="executing",
                            active_job=refreshed,
                        )
                    except Exception as exc:
                        self._record_event("warning", "worker_heartbeat_error", type(exc).__name__)
                        return
                    heartbeat_stop.wait(30)
            finally:
                heartbeat_state.close()

        heartbeat = threading.Thread(
            target=maintain_lease,
            name=f"{self.agent.agent_id}-worker-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            if self.agent.runtime == "codex":
                self._execute_codex(executing, token, project, topic)
            else:
                self._execute_external(executing, token, project, topic)
        except Exception as exc:
            try:
                if isinstance(exc, ProviderLimitError):
                    self._last_error_code = "provider_limit"
                    self._provider_state = "limited"
                    self._quota_remaining_percent = float(exc.limit.remaining_percent)
                    self._quota_reset_at = datetime.fromtimestamp(exc.limit.resets_at, timezone.utc)
                    self.state.fail_provider_job(
                        executing.job_id,
                        token,
                        error_class="quota",
                        error_code=type(exc).__name__,
                    )
                    self._record_event("warning", "provider_limit", exc.limit.to_json())
                else:
                    self._last_error_code = type(exc).__name__[:128]
                    self._provider_state = "unavailable"
                    # Invocation has been marked executing; no automatic replay
                    # is safe without runtime-specific proof that it never began.
                    self.state.mark_provider_job_indeterminate(
                        executing.job_id, token, error_code=type(exc).__name__
                    )
                    self._record_event(
                        "warning",
                        "queued_provider_error",
                        f"ambiguous_execution:{type(exc).__name__}",
                    )
            except Exception:
                pass
            if self.agent.runtime == "codex":
                self._discard_client()
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)

    def _commit(
        self,
        job: ProviderJobRecord,
        token: str,
        *,
        visible_response: str,
        provider_session_id: str | None,
        actual_model: str | None,
        telegram_html: str,
    ) -> None:
        self.state.commit_provider_result(
            job.job_id,
            token,
            visible_response=visible_response[:12000],
            sender_agent_id=self.agent.agent_id,
            telegram_html=telegram_html[:4090],
            provider_session_id=provider_session_id,
            actual_model=actual_model,
            user_excerpt=job.payload_text,
            acknowledge_context=job.context_watermark is not None,
            acknowledge_handoff=job.handoff_id is not None,
        )

    def _execute_codex(
        self, job: ProviderJobRecord, token: str, project: object, topic: object
    ) -> None:
        from .registry import Project
        from .state import TopicRecord

        assert isinstance(project, Project)
        assert isinstance(topic, TopicRecord)
        client = self._client()
        if job.provider_session_id:
            thread = client.resume_thread(
                thread_id=job.provider_session_id, cwd=project.root, model=job.model
            )
        else:
            thread = client.start_thread(
                cwd=project.root, model=job.model, project_id=project.project_id
            )
        turn_id = client.start_turn(
            thread_id=thread.thread_id,
            cwd=project.root,
            text=job.payload_text,
            model=job.model,
            effort=job.effort,
        )
        result = client.wait_for_turn(turn_id)
        visible_response = result.text.strip() or "Codex completed the turn without visible text."
        if result.context_window and result.context_tokens_used is not None:
            try:
                self.state.set_context_remaining(
                    job.session_id,
                    max(0, result.context_window - result.context_tokens_used)
                    * 100
                    / result.context_window,
                )
            except Exception:
                pass
        try:
            limits = client.read_rate_limits()
        except Exception:
            limits = RateLimits(None, None)
        self._commit(
            job,
            token,
            visible_response=visible_response,
            provider_session_id=thread.thread_id,
            actual_model=thread.model,
            telegram_html=format_telegram_response(
                result=result,
                agent=self.agent.display_name,
                model=thread.model,
                effort=job.effort,
                session_label=f"{project.display_name} · {topic.title} · {self.agent.display_name}",
                limits=limits,
                timezone_name="Europe/Moscow",
            ),
        )

    def _execute_external(
        self, job: ProviderJobRecord, token: str, project: object, topic: object
    ) -> None:
        from .registry import Project
        from .state import TopicRecord

        assert isinstance(project, Project)
        assert isinstance(topic, TopicRecord)
        assert self.adapter is not None
        result = self.adapter.run_turn(
            cwd=project.root,
            prompt=job.payload_text,
            session_id=job.provider_session_id,
            model=job.model if job.model != "provider-selected" else None,
            effort=job.effort,
        )
        if job.provider_session_id is None and result.provider_session_id is None:
            raise ExternalRuntimeError(
                f"{self.agent.runtime} did not return a provider session id for a new turn"
            )
        visible_response = result.text.strip()
        self._commit(
            job,
            token,
            visible_response=visible_response,
            provider_session_id=result.provider_session_id,
            actual_model=result.model or job.model,
            telegram_html=format_agent_response(
                visible_response,
                {
                    "Session": f"{project.display_name} · {topic.title} · {self.agent.display_name}",
                    "Agent": self.agent.display_name,
                    "Runtime": self.agent.runtime,
                    "Model": result.model or job.model,
                    "Effort": job.effort,
                    "Context remaining": "unavailable",
                    "Usage windows": "unavailable",
                },
            ),
        )
