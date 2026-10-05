from __future__ import annotations

import os
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from .claude_recovery import recover_claude_completion, recover_claude_job
from .claude_stream import (
    ClaudeStreamError,
    ClaudeTerminalFailure,
    ClaudeVisibleAssistant,
    VisibleAssistantCallback,
)
from .codex_appserver import (
    CodexAppServerClient,
    RateLimits,
    RpcRejectedError,
    context_remaining_percent,
)
from .codex_failure import codex_preparation, uncertain_provider_notice
from .codex_recovery import (
    checkpoint_failure_notice,
    reconcile_codex_completion,
    recover_codex_job,
)
from .controller_result_publication import (
    PreparedResultPublication,
    PreparedResultPublisher,
)
from .diagnostic_log import survived
from .execution_journal import ExecutionJournal
from .external_runtime import (
    ExternalCliAdapter,
    ExternalRuntimeError,
    ProviderLimitError,
    ProviderUnavailableError,
)
from .hub_config import HubConfig
from .project_resolution import (
    ProjectResolutionError,
    resolve_project_context,
    resolve_project_group,
)
from .registry import ExecutionRootError, ProjectRegistry, load_registry
from .session_adoption_policy import validate_adoption_mode
from .session_adoption_state import CodexSessionOrigins
from .session_connect import ConnectCandidate, SessionConnectStore
from .state import HubState, ProviderJobRecord, StateError
from .supervisor import CodexAppServerSupervisor
from .telegram_interaction import (
    telegram_contract_version,
    telegram_developer_instructions,
)
from .turn_observation import TurnObservation
from .worker_activity import codex_activity_for_turn
from .worker_execution import (
    ProviderSessionPreparationError,
    ProviderTurnStopped,
    WorkerFailureClassification,
    classify_worker_failure,
    codex_provider_prompt,
    codex_turn_text,
    external_provider_prompt,
    invoke_external_provider_turn,
    open_codex_provider_thread,
    prepare_codex_worker_result,
    prepare_external_worker_result,
    prepare_worker_artifacts,
    prepare_worker_materials,
    prepare_worker_staging_directory,
    require_provider_job_lease,
    resolve_external_worker_target,
    revalidate_worker_execution_root,
    start_codex_provider_turn,
    wait_for_codex_provider_turn,
    worker_needs_full_telegram_contract,
)


class ExternalQueueWorkerError(RuntimeError):
    pass


class ExternalQueueWorker:
    """One provider-scoped queue worker with no Telegram transport capability."""

    _LOCAL_RUNTIMES = frozenset({"codex", "claude", "opencode", "antigravity"})

    def __init__(
        self,
        config: HubConfig,
        agent_id: str = "codex",
        *,
        registry: ProjectRegistry | None = None,
        supervisor: CodexAppServerSupervisor | None = None,
        adapter: ExternalCliAdapter | None = None,
        worker_id: str | None = None,
        worker_slot: int = 1,
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
                "external worker supports codex, claude, opencode, and antigravity"
            )
        if self.agent.managed_externally:
            raise ExternalQueueWorkerError("external worker agent must be locally managed")
        if self.agent.runtime == "codex" and worker_slot > 1 and config.manage_codex_server:
            raise ExternalQueueWorkerError(
                "multiple Codex slots require a separately managed server"
            )
        if worker_slot < 1 or worker_slot > config.worker_count_for_agent(agent_id):
            raise ExternalQueueWorkerError("worker slot is not configured for this agent")
        validate_adoption_mode(config)
        self.registry = registry or load_registry(config.registry_path)
        self.state = HubState.open(config.state_path)
        try:
            self.state.reconcile_legacy_execution_scopes(
                {project.project_id: project.root for project in self.registry.projects}
            )
        except BaseException:
            self.state.close()
            raise
        self.worker_id = worker_id or (
            f"{self.agent.agent_id}-worker"
            if worker_slot == 1
            else f"{self.agent.agent_id}-worker-{worker_slot}"
        )
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
                model_provider=config.codex_model_provider,
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
            except Exception as survived_error:
                survived("external_worker.client_close", survived_error)

    def _restore_codex_socket_at_idle(self) -> None:
        # The productive worker calls this only between run_cycle invocations.
        # Its cached stdio client may retain a different native writer, so close
        # it only after the shared socket has answered a metadata handshake.
        if self.supervisor is not None and self.supervisor.restore_socket_at_idle():
            self._discard_client()

    def _record_event(self, level: str, code: str, detail: str) -> None:
        try:
            event_state = HubState.open(self.config.state_path)
            try:
                event_state.record_runtime_event(self.agent.agent_id, level, code, detail)
            finally:
                event_state.close()
        except Exception as survived_error:
            survived("external_worker.runtime_event_record", survived_error)

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
                error_code=(
                    self._last_error_code
                    or (
                        "codex_approvals_unavailable"
                        if self.supervisor is not None
                        and self.supervisor.transport_mode == "stdio-fallback"
                        else None
                    )
                ),
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
        except Exception as survived_error:
            survived("external_worker.health_publish", survived_error)

    def run_forever(self, *, poll_seconds: float = 0.2) -> None:
        if poll_seconds <= 0:
            raise ExternalQueueWorkerError("poll_seconds must be positive")
        if self.supervisor is not None:
            self.supervisor.start()
        connect_thread: threading.Thread | None = None
        if self.agent.runtime == "codex":
            connect_thread = threading.Thread(
                target=self._run_connect_forever,
                name=f"{self.worker_id}-connect",
                daemon=True,
            )
            connect_thread.start()
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
        finally:
            self._stop.set()
            if connect_thread is not None:
                connect_thread.join(timeout=40)

    def _run_connect_forever(self) -> None:
        """Serve metadata requests while the productive worker waits on a turn."""
        while not self._stop.is_set():
            try:
                state = HubState.open(self.config.state_path)
                try:
                    while not self._stop.is_set():
                        worked = self._run_connect_cycle(state=state)
                        self._stop.wait(0.01 if worked else 0.2)
                finally:
                    state.close()
            except Exception as exc:
                self._record_event("error", "connect_worker_cycle_error", type(exc).__name__)
                self._stop.wait(1)

    def run_cycle(self) -> bool:
        """Lease and execute at most one job for this worker's sole agent."""
        if self._stop.is_set():
            return False
        if isinstance(self.supervisor, CodexAppServerSupervisor):
            self._restore_codex_socket_at_idle()
        self._publish_health()
        if self.agent.runtime == "codex":
            assert self.supervisor is not None
            if recover_codex_job(
                self.state,
                self.config,
                self.registry,
                self.agent.agent_id,
                self.worker_id,
                self.supervisor.client,
            ):
                return True
            if TurnObservation(self.state, self.config).run_once(self.supervisor.client):
                return True
        if self.agent.runtime == "claude" and recover_claude_job(
            self.state, self.config, self.registry, self.agent.agent_id, self.worker_id
        ):
            return True
        self.state.recover_stale_provider_jobs(agent_id=self.agent.agent_id)
        if self._stop.is_set():
            return False
        job = self.state.lease_provider_job(
            self.agent.agent_id,
            self.worker_id,
            max_parallel_roots=self.config.max_parallel_roots,
            scheduler_agents=self.config.external_worker_agent_ids,
            agent_capacities={
                "codex": self.config.codex_worker_count,
                "claude": self.config.claude_worker_count,
            },
        )
        if job is None:
            return self._run_connect_cycle() if self.agent.runtime == "codex" else False
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

    def _run_connect_cycle(self, *, state: HubState | None = None) -> bool:
        """Handle one leased metadata request with an independent Codex client."""
        current_state = state or self.state
        store = SessionConnectStore(current_state)
        workflow = store.lease_worker(self.worker_id)
        if workflow is None:
            return False
        client: CodexAppServerClient | None = None
        try:
            if workflow.canonical_root is None:
                raise ExternalQueueWorkerError("connect project is missing")
            if workflow.project_id is None:
                raise ExternalQueueWorkerError("connect project is missing")
            if workflow.destination_chat_id is not None:
                resolve_project_context(
                    self.config,
                    current_state,
                    chat_id=workflow.destination_chat_id,
                    expected_project_id=workflow.project_id,
                    expected_root=workflow.canonical_root,
                )
            else:
                resolve_project_group(
                    self.config,
                    current_state,
                    project_id=workflow.project_id,
                    expected_root=workflow.canonical_root,
                )
            assert self.supervisor is not None
            client = self.supervisor.client(allow_fallback=False)
            client.initialize()
            if workflow.stage == "discovering":
                discovered = client.list_connectable_threads(root=workflow.canonical_root)
                store.finish_discovery(
                    workflow.workflow_id,
                    workflow.lease_token,
                    tuple(
                        ConnectCandidate("", item.thread_id, item.safe_label, item.updated_at)
                        for item in discovered
                    ),
                )
            elif workflow.stage == "activation_requested":
                if workflow.source_thread_id is None:
                    raise ExternalQueueWorkerError("connect source is missing")
                metadata = client.read_thread_metadata(
                    thread_id=workflow.source_thread_id,
                    cwd=workflow.canonical_root,
                )
                store.prepare_marker(
                    workflow.workflow_id,
                    workflow.lease_token,
                    model_provider=metadata.model_provider,
                )
            else:
                raise ExternalQueueWorkerError("connect workflow stage is not executable")
        except Exception as exc:
            safe_code = (
                str(exc) if type(exc).__name__ == "CodexMetadataError" else "metadata_unavailable"
            )
            store.fail_worker(workflow.workflow_id, workflow.lease_token, safe_code)
        finally:
            if client is not None:
                client.close()
        return True

    def _execute(self, job: ProviderJobRecord) -> None:
        lease_token = require_provider_job_lease(job, error_factory=ExternalQueueWorkerError)
        try:
            target = resolve_external_worker_target(self.config, self.state, job)
        except ProjectResolutionError as exc:
            self.state.terminate_provider_job_with_notice(
                job.job_id,
                lease_token,
                status="failed",
                expected_status="leased",
                error_class="pre_execution",
                error_code=str(exc),
                sender_agent_id=self.agent.agent_id,
                telegram_html=(
                    f"{self.agent.display_name} did not start: the project binding is invalid; verify it locally."
                ),
            )
            self._last_error_code = str(exc)[:128]
            self._provider_state = "unavailable"
            return
        self.registry = target.registry
        project = target.project
        topic = target.topic
        executing = self.state.mark_provider_job_executing(job.job_id, lease_token, honor_stop=True)
        if executing.status == "cancelled":
            self._record_event("info", "provider_turn_stopped", self.agent.agent_id)
            return
        self._publish_health(activity_state="executing", active_job=executing)
        token = executing.lease_token
        assert token is not None
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
            target = revalidate_worker_execution_root(self.state, target)
            project = target.project
            if self.agent.runtime == "codex":
                self._execute_codex(executing, token, project, topic)
            else:
                self._execute_external(executing, token, project, topic)
        except Exception as exc:
            self._record_failure(executing, token, Path(project.root), exc)
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
            self._cleanup_incoming_material_staging(Path(project.root), executing.job_id)

    def _record_failure(
        self, executing: ProviderJobRecord, token: str, project_root: Path, exc: Exception
    ) -> None:
        """Commit the durable outcome of a failed execution; never raises.

        A covering stop can win the result, recovery or failure commit (R-021);
        the job is then already cancelled and the outcome is a stopped turn.
        """
        failure = classify_worker_failure(exc, runtime=self.agent.runtime)
        try:
            if self.agent.runtime == "claude" and recover_claude_completion(
                self.state,
                self.config,
                self.registry,
                self.agent.agent_id,
                executing.job_id,
                token,
            ):
                outcome = self.state.get_provider_job(executing.job_id)
                self._last_error_code = (
                    "claude_result_recovery_pending" if outcome.status == "executing" else None
                )
                self._provider_state = (
                    "ready" if outcome.status in {"result_ready", "cancelled"} else "unavailable"
                )
                return
            if failure.notice in {"provider_session_preparation", "claude_terminal"}:
                assert isinstance(exc, (ProviderSessionPreparationError, ClaudeTerminalFailure))
                self._record_known_claude_failure(executing, token, project_root, exc, failure)
            elif failure.notice == "execution_root":
                assert isinstance(exc, ExecutionRootError)
                self._last_error_code = failure.error_code
                self._provider_state = "unavailable"
                self.state.terminate_provider_job_with_notice(
                    executing.job_id,
                    token,
                    status=failure.status,
                    error_class=failure.error_class,
                    error_code=failure.error_code,
                    sender_agent_id=self.agent.agent_id,
                    telegram_html=exc.public_message,
                )
            elif failure.notice == "emergency_stop":
                assert isinstance(exc, ProviderTurnStopped)
                self.state.cancel_active_provider_job(
                    executing.job_id,
                    token,
                    error_code=failure.error_code,
                    complete_stops=True,
                )
                self._last_error_code = None
                self._provider_state = "ready"
                self._record_event("info", "provider_turn_stopped", self.agent.agent_id)
            elif failure.notice == "provider_limit":
                assert isinstance(exc, ProviderLimitError)
                self._last_error_code = "provider_limit"
                self._provider_state = "limited"
                self._quota_remaining_percent = float(exc.limit.remaining_percent)
                self._quota_reset_at = datetime.fromtimestamp(exc.limit.resets_at, timezone.utc)
                self.state.terminate_provider_job_with_notice(
                    executing.job_id,
                    token,
                    status=failure.status,
                    error_class=failure.error_class,
                    error_code=failure.error_code,
                    sender_agent_id=self.agent.agent_id,
                    telegram_html=(
                        f"{self.agent.display_name} limit reached. Reset telemetry was "
                        "recorded; use /accounts for the current status."
                    ),
                )
                self._record_event("warning", "provider_limit", exc.limit.to_json())
            elif failure.notice == "provider_unavailable":
                assert isinstance(exc, ProviderUnavailableError)
                self._last_error_code = failure.error_code
                self._provider_state = "unavailable"
                self.state.terminate_provider_job_with_notice(
                    executing.job_id,
                    token,
                    status=failure.status,
                    error_class=failure.error_class,
                    error_code=failure.error_code,
                    sender_agent_id=self.agent.agent_id,
                    telegram_html=exc.public_message,
                )
                self._record_event("warning", "provider_unavailable", failure.error_code)
            else:
                recovered = False
                turn_status = "unknown"
                if failure.reconcile_codex:
                    assert self.supervisor is not None
                    try:
                        turn_status = reconcile_codex_completion(
                            self.state,
                            self.config,
                            project_root=project_root,
                            job_id=executing.job_id,
                            lease_token=token,
                            agent_id=self.agent.agent_id,
                            client_factory=self.supervisor.client,
                        )
                        recovered = turn_status == "completed"
                    except ProviderTurnStopped:  # won the recovery commit (R-021)
                        turn_status = "stopped"
                    except Exception:
                        turn_status = "unknown"
                if recovered:
                    self._last_success_at = datetime.now(timezone.utc)
                    self._last_error_code = None
                    self._provider_state = "ready"
                    self._record_event("info", "provider_result_recovered", self.agent.agent_id)
                elif turn_status == "stopped":  # the stop already cancelled the job
                    self._last_error_code = None
                    self._provider_state = "ready"
                    self._record_event("info", "provider_turn_stopped", self.agent.agent_id)
                else:
                    self._last_error_code = type(exc).__name__[:128]
                    self._provider_state = "unavailable"
                    # Keep the provider's bounded diagnostic in the private state DB.
                    # Without it every app-server protocol or quota failure collapses
                    # to an unhelpful ``RpcError`` and cannot be repaired remotely.
                    error_detail = " ".join(str(exc).split())[:1000] or None
                    # Invocation has been marked executing; no automatic replay
                    # is safe without runtime-specific proof that it never began.
                    record = self.state.terminate_provider_job_with_notice(
                        executing.job_id,
                        token,
                        status=failure.status,
                        error_class=failure.error_class,
                        error_code=failure.error_code,
                        error_detail=error_detail,
                        terminal_turn_status=(
                            turn_status if turn_status in {"failed", "interrupted"} else None
                        ),
                        sender_agent_id=self.agent.agent_id,
                        telegram_html=(
                            "Incoming material integrity validation failed; "
                            "the provider was not started. Send the material again."
                            if failure.notice == "incoming_material"
                            else checkpoint_failure_notice(
                                self.state, executing.job_id, exc, turn_status=turn_status
                            )
                            if failure.notice == "checkpoint"
                            else self._claude_partial_notice(
                                executing,
                                token,
                                project_root,
                                uncertain_provider_notice(self.agent.display_name),
                            )
                        ),
                    )
                    if record.status == "cancelled":  # a covering stop won the commit
                        self._last_error_code = None
                        self._provider_state = "ready"
                        self._record_event("info", "provider_turn_stopped", self.agent.agent_id)
                    else:
                        self._record_event(
                            "warning",
                            "queued_provider_error",
                            f"{failure.error_class}:{failure.error_code}",
                        )
        except Exception as survived_error:
            survived("external_worker.failure_notice_record", survived_error)
        if self.agent.runtime == "codex":
            self._discard_client()

    def _record_known_claude_failure(
        self,
        job: ProviderJobRecord,
        token: str,
        project_root: Path,
        error: ProviderSessionPreparationError | ClaudeTerminalFailure,
        failure: WorkerFailureClassification,
    ) -> None:
        """Publish a verified failure once; a covering stop owns cancellation."""
        record = self.state.terminate_provider_job_with_notice(
            job.job_id,
            token,
            status=failure.status,
            error_class=failure.error_class,
            error_code=failure.error_code,
            sender_agent_id=self.agent.agent_id,
            telegram_html=(
                self._claude_partial_notice(job, token, project_root, error.public_message)
                if isinstance(error, ClaudeTerminalFailure)
                else error.public_message
            ),
        )
        self._quota_remaining_percent = None
        self._quota_reset_at = None
        if record.status == "cancelled":
            self._last_error_code = None
            self._provider_state = "ready"
            self._record_event("info", "provider_turn_stopped", self.agent.agent_id)
            return
        self._last_error_code = failure.error_code
        self._provider_state = "limited" if failure.error_class == "quota" else "unavailable"
        self._record_event("warning", "claude_provider_failure", failure.error_code)

    def _claude_partial_notice(
        self, job: ProviderJobRecord, token: str, project_root: Path, notice: str
    ) -> str:
        """Expose provisional text only while its invocation binding is still valid."""
        if self.agent.runtime != "claude":
            return notice
        try:
            journal = ExecutionJournal(self.state)
            checkpoint = journal.read(job.job_id)
            if checkpoint is None or checkpoint["provider_thread_id"] is None:
                return notice
            partial = journal.validated_claude_partial(
                job.job_id, token, checkpoint["provider_thread_id"], cwd=project_root
            )
        except Exception as exc:
            survived("external_worker.claude_partial_binding", exc)
            return notice
        if not partial:
            return notice
        return notice + "\n\nPartial response (incomplete):\n" + escape(partial)

    def _cleanup_incoming_material_staging(self, project_root: Path, job_id: str) -> None:
        directory = project_root / ".hub" / "incoming" / job_id
        try:
            if not directory.exists() and not directory.is_symlink():
                return
            if directory.is_symlink() or not directory.is_dir():
                directory.unlink(missing_ok=True)
                return
            for path in directory.iterdir():
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
            directory.rmdir()
        except OSError as exc:
            self._record_event("warning", "incoming_staging_cleanup_error", type(exc).__name__)

    def _needs_full_telegram_contract(self, job: ProviderJobRecord) -> bool:
        return worker_needs_full_telegram_contract(self.state, job, self.agent.runtime)

    def _execute_codex(
        self, job: ProviderJobRecord, token: str, project: object, topic: object
    ) -> None:
        from .registry import Project
        from .state import TopicRecord

        assert isinstance(project, Project)
        assert isinstance(topic, TopicRecord)
        assert self.supervisor is not None
        prepared = prepare_worker_materials(
            self.state,
            state_path=self.config.state_path,
            execution_root=Path(project.root),
            job=job,
            runtime="codex",
        )
        journal = ExecutionJournal(
            self.state, progress_enabled=self.config.outbox_runtime == "external"
        )
        with codex_preparation():
            origin = CodexSessionOrigins(self.state).get(job.session_id)
            if origin is not None:
                validate_adoption_mode(self.config, self.state._connection)
                current_session = self.state.get_session(job.session_id)
                root = project.root.resolve(strict=True)
                if (
                    origin.provider_thread_id != job.provider_session_id
                    or current_session.provider_session_id != origin.provider_thread_id
                    or origin.project_id != project.project_id
                    or origin.canonical_root != root
                    or current_session.writer_mode != "telegram"
                    or not any(
                        root.is_relative_to(allowed.resolve(strict=True))
                        for allowed in self.registry.allowed_roots
                    )
                ):
                    raise ExternalQueueWorkerError("adopted Codex binding mismatch")
                git_root = subprocess.run(
                    ("git", "-C", str(root), "rev-parse", "--show-toplevel"),
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=True,
                )
                if Path(git_root.stdout.strip()).resolve(strict=True) != root:
                    raise ExternalQueueWorkerError("adopted Codex project root mismatch")
            client = self._client()
            if origin is not None:
                metadata = client.read_thread_metadata(
                    thread_id=origin.provider_thread_id, cwd=origin.canonical_root
                )
                if (
                    metadata.thread_id != origin.provider_thread_id
                    or metadata.cwd != origin.canonical_root
                    or metadata.model_provider
                    not in {origin.model_provider, self.config.codex_model_provider}
                ):
                    raise ExternalQueueWorkerError("adopted Codex source mismatch")
            fallback_transfer = bool(
                origin is None
                and self.config.codex_model_provider is None
                and job.provider_session_id
                and self.supervisor.transport_mode == "stdio-fallback"
            )
            if fallback_transfer and job.idempotency_key.startswith("continuation:"):
                raise ExternalQueueWorkerError(
                    "continuation requires the owning Codex socket; fallback cannot preserve its thread"
                )
            turn_text = codex_turn_text(job, prepared)
            if fallback_transfer:
                visible_context = self.state.recent_external_context(
                    job.topic_id, self.agent.agent_id, limit=8
                )
                turn_text = codex_turn_text(
                    job,
                    prepared,
                    fallback_visible_context=visible_context,
                )
            staging_dir = prepare_worker_staging_directory(Path(project.root), job.job_id)
            full_contract = self._needs_full_telegram_contract(job) or fallback_transfer
            developer_instructions = telegram_developer_instructions(
                runtime="codex", new_session=full_contract
            )
            thread = open_codex_provider_thread(
                client,
                job,
                project,
                developer_instructions=developer_instructions,
                force_new_thread=fallback_transfer,
            )
            if origin is not None and (
                thread.thread_id != origin.provider_thread_id
                or thread.cwd != origin.canonical_root
                or thread.model_provider
                != (self.config.codex_model_provider or origin.model_provider)
            ):
                raise ExternalQueueWorkerError("adopted Codex resume identity mismatch")
            journal.record_thread(job.job_id, token, thread.thread_id, project.root)
        with codex_activity_for_turn(
            client, self.state, self.config, job.job_id, token, project.root
        ) as accepted_activity:
            turn_id = start_codex_provider_turn(
                client,
                job,
                thread,
                project,
                prompt=codex_provider_prompt(turn_text, staging_dir=staging_dir),
                local_image_paths=prepared.local_image_paths,
            )
            journal.record_turn(job.job_id, token, turn_id)
            accepted_activity(thread.thread_id, turn_id)
            client.on_visible_item = lambda item_id, text, phase: journal.record_item(
                job.job_id, token, item_id, text, phase
            )
            client.on_completed = lambda result: journal.record_completion(
                job.job_id, token, result.text
            )
            monitor_stop, turn_transport_mode = threading.Event(), self.supervisor.transport_mode
            interrupted_request: list[str] = []

            def monitor_control() -> None:
                monitor_state = HubState.open(self.config.state_path)
                try:
                    while not monitor_stop.wait(0.2):
                        request_id = monitor_state.pending_emergency_stop_for_job(job.job_id)
                        if request_id is not None:
                            try:
                                assert self.supervisor is not None
                                if turn_transport_mode == "stdio-fallback":
                                    client.close()
                                else:
                                    interrupt_client = self.supervisor.client(allow_fallback=False)
                                    try:
                                        interrupt_client.interrupt_turn(
                                            thread_id=thread.thread_id, turn_id=turn_id
                                        )
                                    finally:
                                        interrupt_client.close()
                            except Exception as exc:
                                self._record_event(
                                    "warning", "provider_interrupt_unconfirmed", type(exc).__name__
                                )
                            else:
                                interrupted_request.append(request_id)
                            return
                        assert self.supervisor is not None
                        if turn_transport_mode == "stdio-fallback":
                            # A fallback client owns a private app-server process;
                            # a second client cannot address its active turn.
                            continue
                        self._steer_ready_followup(monitor_state, job, thread.thread_id, turn_id)
                finally:
                    monitor_state.close()

            monitor = threading.Thread(
                target=monitor_control,
                name="codex-live-control",
                daemon=True,
            )
            monitor.start()
            try:
                result = wait_for_codex_provider_turn(client, turn_id)
                journal.record_completion(job.job_id, token, result.text)
            finally:
                client.on_visible_item = None
                client.on_completed = None
                monitor_stop.set()
                monitor.join(timeout=2)
        late_request = self.state.pending_emergency_stop_for_job(job.job_id)
        if interrupted_request:
            raise ProviderTurnStopped(interrupted_request[0])
        if late_request is not None:
            raise ProviderTurnStopped(late_request)
        try:
            self.state.set_context_remaining(job.session_id, context_remaining_percent(result))
        except Exception as survived_error:
            survived("external_worker.context_telemetry", survived_error)
        try:
            limits = client.read_rate_limits()
        except Exception:
            limits = RateLimits(None, None)
        artifacts = prepare_worker_artifacts(
            Path(project.root),
            job.job_id,
            self.config.state_path,
            report_rejections=True,
        )
        prepared_result = prepare_codex_worker_result(
            result,
            prepared,
            agent_name=self.agent.display_name,
            model=thread.model,
            effort=job.effort,
            session_label=(f"{project.display_name} · {topic.title} · {self.agent.display_name}"),
            limits=limits,
            artifact_notice=artifacts.visible_notice,
            trim_visible_text=True,
            empty_visible_text="Codex completed the turn without visible text.",
        )
        PreparedResultPublisher(
            state=self.state,
            state_path=self.config.state_path,
            cleanup_error=lambda code, detail: self._record_event("warning", code, detail),
        ).publish(
            PreparedResultPublication(
                job=job,
                project_root=Path(project.root),
                prepared_materials=prepared,
                visible_response=prepared_result.visible_response,
                telegram_html=prepared_result.telegram_html,
                provider_session_id=thread.thread_id,
                actual_model=thread.model,
                telegram_contract_version=telegram_contract_version(self.agent.runtime),
                artifacts=artifacts.artifacts,
            )
        )

    def _steer_ready_followup(
        self, state: HubState, job: ProviderJobRecord, thread_id: str, turn_id: str
    ) -> None:
        """Steer the next compatible queued message into the running Codex turn.

        The follow-up starts only through ``start_steer_followup``, which honors
        a pending emergency stop in the same transaction, so a follow-up that
        is cancelled or returned to the queue never reaches the provider.
        """
        followup = state.lease_steer_followup(
            job.job_id, f"{self.worker_id}-steer", lease_seconds=120
        )
        if followup is None or followup.lease_token is None:
            return
        steer_token = followup.lease_token
        started = state.start_steer_followup(followup.job_id, steer_token, parent_job_id=job.job_id)
        if started.status != "executing":
            return
        assert self.supervisor is not None
        steer_client = None
        try:
            steer_client = self.supervisor.client(allow_fallback=False)
            returned_turn = steer_client.steer_turn(
                thread_id=thread_id,
                turn_id=turn_id,
                text=followup.payload_text,
                client_user_message_id=followup.job_id,
            )
            state.complete_steered_job(
                followup.job_id,
                steer_token,
                parent_job_id=job.job_id,
                provider_turn_id=returned_turn,
            )
        except RpcRejectedError:
            state.reject_unaccepted_steer(followup.job_id, steer_token)
        except Exception as exc:
            state.mark_provider_job_indeterminate(
                followup.job_id,
                steer_token,
                error_code=type(exc).__name__,
                error_detail="same-turn steering outcome is unknown",
            )
        finally:
            if steer_client is not None:
                try:
                    steer_client.close()
                except Exception as survived_error:
                    survived("external_worker.steer_client_close", survived_error)

    def _execute_external(
        self, job: ProviderJobRecord, token: str, project: object, topic: object
    ) -> None:
        from .registry import Project
        from .state import TopicRecord

        assert isinstance(project, Project)
        assert isinstance(topic, TopicRecord)
        assert self.adapter is not None
        adapter = self.adapter
        prepared = prepare_worker_materials(
            self.state,
            state_path=self.config.state_path,
            execution_root=Path(project.root),
            job=job,
            runtime=self.agent.runtime,
        )
        staging_dir = prepare_worker_staging_directory(Path(project.root), job.job_id)
        claude_session_binding = None
        claude_journal = None
        on_visible_assistant: VisibleAssistantCallback | None = None
        if self.agent.runtime == "claude":
            try:
                claude_journal = ExecutionJournal(self.state)
                claude_session_binding = claude_journal.prepare_claude_session(
                    job.job_id, token, Path(project.root)
                )
            except (StateError, OSError) as exc:
                raise ProviderSessionPreparationError() from exc

            def record_visible_assistant(item: ClaudeVisibleAssistant) -> None:
                assert claude_journal is not None
                assert claude_session_binding is not None
                if item.session_id != claude_session_binding.session_id:
                    raise ClaudeStreamError("claude visible message has a different session")
                try:
                    claude_journal.record_claude_item(
                        job.job_id,
                        token,
                        claude_session_binding.session_id,
                        item.message_id,
                        item.text,
                        cwd=Path(project.root),
                    )
                except Exception as exc:
                    raise ClaudeStreamError("claude visible message could not be saved") from exc

            on_visible_assistant = record_visible_assistant
        prepare_interrupt = getattr(adapter, "prepare_interruptible_turn", None)
        interrupt_prepared = callable(prepare_interrupt)
        if interrupt_prepared:
            prepare_interrupt()
        monitor_stop = threading.Event()
        interrupted_request: list[str] = []

        def monitor_interrupt() -> None:
            monitor_state = HubState.open(self.config.state_path)
            try:
                while not monitor_stop.wait(0.2):
                    request_id = monitor_state.pending_emergency_stop_for_job(job.job_id)
                    if request_id is None:
                        continue
                    try:
                        adapter.interrupt()
                    except Exception as exc:
                        self._record_event(
                            "warning", "provider_interrupt_unconfirmed", type(exc).__name__
                        )
                    else:
                        interrupted_request.append(request_id)
                    return
            finally:
                monitor_state.close()

        monitor = threading.Thread(
            target=monitor_interrupt,
            name=f"{self.agent.agent_id}-emergency-stop",
            daemon=True,
        )
        monitor.start()
        try:
            from .claude_permission_host import hosted_claude_launch

            with hosted_claude_launch(
                self.config,
                self.state,
                self.agent,
                job,
                token,
                claude_session_binding.session_id if claude_session_binding else None,
                Path(project.root),
                is_new=bool(claude_session_binding and claude_session_binding.is_new),
            ) as hosted:
                result = invoke_external_provider_turn(
                    adapter,
                    job,
                    project,
                    prompt=external_provider_prompt(
                        job,
                        prepared,
                        runtime=self.agent.runtime,
                        full_contract=self._needs_full_telegram_contract(job),
                        staging_dir=staging_dir,
                    ),
                    interrupt_prepared=interrupt_prepared,
                    staging_dir=staging_dir,
                    claude_session_binding=claude_session_binding,
                    on_visible_assistant=on_visible_assistant,
                    claude_sandbox=hosted.sandbox if hosted else None,
                )
            if claude_journal is not None and claude_session_binding is not None:
                try:
                    claude_journal.record_claude_completion(
                        job.job_id,
                        token,
                        claude_session_binding.session_id,
                        result.text,
                        cwd=Path(project.root),
                    )
                except Exception as exc:
                    raise ClaudeStreamError("claude completion could not be saved") from exc
        finally:
            monitor_stop.set()
            monitor.join(timeout=2)
        late_request = self.state.pending_emergency_stop_for_job(job.job_id)
        if interrupted_request:
            raise ProviderTurnStopped(interrupted_request[0])
        if late_request is not None:
            raise ProviderTurnStopped(late_request)
        if job.provider_session_id is None and result.provider_session_id is None:
            raise ExternalRuntimeError(
                f"{self.agent.runtime} did not return a provider session id for a new turn"
            )
        artifacts = prepare_worker_artifacts(
            Path(project.root),
            job.job_id,
            self.config.state_path,
            report_rejections=True,
        )
        actual_model = result.model or job.model
        prepared_result = prepare_external_worker_result(
            result,
            prepared,
            agent_name=self.agent.display_name,
            runtime=self.agent.runtime,
            model=actual_model,
            effort=job.effort,
            session_label=(f"{project.display_name} · {topic.title} · {self.agent.display_name}"),
            artifact_notice=artifacts.visible_notice,
            trim_visible_text=True,
        )
        PreparedResultPublisher(
            state=self.state,
            state_path=self.config.state_path,
            cleanup_error=lambda code, detail: self._record_event("warning", code, detail),
        ).publish(
            PreparedResultPublication(
                job=job,
                project_root=Path(project.root),
                prepared_materials=prepared,
                visible_response=prepared_result.visible_response,
                telegram_html=prepared_result.telegram_html,
                provider_session_id=result.provider_session_id,
                actual_model=actual_model,
                telegram_contract_version=telegram_contract_version(self.agent.runtime),
                artifacts=artifacts.artifacts,
            )
        )
