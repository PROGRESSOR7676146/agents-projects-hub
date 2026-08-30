from __future__ import annotations

import threading

from .codex_appserver import CodexAppServerClient, RateLimits
from .external_runtime import ProviderLimitError
from .hub_config import HubConfig
from .metadata import format_telegram_response
from .registry import ProjectRegistry, load_registry
from .state import HubState, ProviderJobRecord
from .supervisor import CodexAppServerSupervisor


class CodexWorkerError(RuntimeError):
    pass


class CodexQueueWorker:
    """Execute durable Codex jobs without any Telegram transport capability."""

    def __init__(
        self,
        config: HubConfig,
        *,
        registry: ProjectRegistry | None = None,
        supervisor: CodexAppServerSupervisor | None = None,
        worker_id: str = "codex-worker",
    ) -> None:
        if config.dispatch_mode != "queue" or config.queue_runtime != "external":
            raise CodexWorkerError("Codex worker requires queue dispatch with external runtime")
        self.config = config
        self.agent = config.require_agent("codex")
        if self.agent.runtime != "codex":
            raise CodexWorkerError("configured codex worker agent must use the Codex runtime")
        self.registry = registry or load_registry(config.registry_path)
        # This connection belongs only to the worker process. Lease heartbeats
        # intentionally use an additional short-lived connection below.
        self.state = HubState.open(config.state_path)
        self.worker_id = worker_id
        self.supervisor = supervisor or CodexAppServerSupervisor(
            config.codex_socket_path,
            manage_process=config.manage_codex_server,
            stdio_executable=config.codex_stdio_executable,
        )
        self._codex_client: CodexAppServerClient | None = None
        self._stop = threading.Event()

    def close(self) -> None:
        self.stop()
        if self._codex_client is not None:
            self._codex_client.close()
            self._codex_client = None
        self.supervisor.stop()
        self.state.close()

    def stop(self) -> None:
        """Request a clean stop between deterministic worker cycles."""
        self._stop.set()

    def _client(self) -> CodexAppServerClient:
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

    def run_forever(self, *, poll_seconds: float = 0.2) -> None:
        if poll_seconds <= 0:
            raise CodexWorkerError("poll_seconds must be positive")
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
        """Lease and execute at most one Codex job; never deliver Telegram outbox."""
        self.state.recover_stale_provider_jobs(agent_id=self.agent.agent_id)
        job = self.state.lease_provider_job(self.agent.agent_id, self.worker_id)
        if job is None:
            return False
        self._execute(job)
        return True

    def _execute(self, job: ProviderJobRecord) -> None:
        if job.lease_token is None:
            raise CodexWorkerError("leased provider job has no lease token")
        executing = self.state.mark_provider_job_executing(job.job_id, job.lease_token)
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
                    except Exception as exc:
                        self._record_event("warning", "worker_heartbeat_error", type(exc).__name__)
                        return
                    heartbeat_stop.wait(30)
            finally:
                heartbeat_state.close()

        heartbeat = threading.Thread(
            target=maintain_lease,
            name="codex-worker-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            client = self._client()
            if executing.provider_session_id:
                thread = client.resume_thread(
                    thread_id=executing.provider_session_id,
                    cwd=project.root,
                    model=executing.model,
                )
            else:
                thread = client.start_thread(
                    cwd=project.root,
                    model=executing.model,
                    project_id=project.project_id,
                )
            turn_id = client.start_turn(
                thread_id=thread.thread_id,
                cwd=project.root,
                text=executing.payload_text,
                model=executing.model,
                effort=executing.effort,
            )
            result = client.wait_for_turn(turn_id)
            visible_response = (
                result.text.strip() or "Codex completed the turn without visible text."
            )
            if result.context_window and result.context_tokens_used is not None:
                remaining = max(0, result.context_window - result.context_tokens_used)
                try:
                    self.state.set_context_remaining(
                        executing.session_id, remaining * 100 / result.context_window
                    )
                except Exception:
                    pass
            try:
                limits = client.read_rate_limits()
            except Exception:
                limits = RateLimits(None, None)
            telegram_html = format_telegram_response(
                result=result,
                agent=self.agent.display_name,
                model=thread.model,
                effort=executing.effort,
                session_label=f"{project.display_name} · {topic.title} · {self.agent.display_name}",
                limits=limits,
                timezone_name="Europe/Moscow",
            )[:4090]
            self.state.commit_provider_result(
                executing.job_id,
                token,
                visible_response=visible_response[:12000],
                sender_agent_id=self.agent.agent_id,
                telegram_html=telegram_html,
                provider_session_id=thread.thread_id,
                actual_model=thread.model,
                user_excerpt=executing.payload_text,
                acknowledge_context=executing.context_watermark is not None,
                acknowledge_handoff=executing.handoff_id is not None,
            )
        except Exception as exc:
            error_class = "quota" if isinstance(exc, ProviderLimitError) else "ambiguous_execution"
            try:
                if isinstance(exc, ProviderLimitError):
                    self.state.fail_provider_job(
                        executing.job_id,
                        token,
                        error_class=error_class,
                        error_code=type(exc).__name__,
                    )
                else:
                    self.state.mark_provider_job_indeterminate(
                        executing.job_id, token, error_code=type(exc).__name__
                    )
            except Exception:
                pass
            self._record_event(
                "warning", "queued_provider_error", f"{error_class}:{type(exc).__name__}"
            )
            self._discard_client()
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
