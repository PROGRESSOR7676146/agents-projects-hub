from __future__ import annotations

import os
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .artifacts import (
    ValidatedArtifact,
    artifact_spool_root,
    cleanup_job_staging,
    remove_spooled_artifact,
)
from .codex_appserver import (
    CodexAppServerClient,
    RateLimits,
    RpcRejectedError,
    context_remaining_percent,
)
from .codex_failure import CodexPreparationError, codex_preparation, uncertain_provider_notice
from .codex_proxy_health import probe_codex_runtime_proxy
from .codex_recovery import (
    checkpoint_failure_notice,
    reconcile_codex_completion,
    recover_codex_job,
)
from .execution_journal import ExecutionJournal
from .external_runtime import (
    ExternalCliAdapter,
    ExternalRuntimeError,
    ExternalTurnInterrupted,
    ProviderLimitError,
    ProviderUnavailableError,
)
from .hub_config import HubConfig
from .incoming_materials import (
    IncomingMaterialError,
    cleanup_consumed_raw_inputs,
    cleanup_materialized_inputs,
)
from .project_resolution import (
    ProjectResolutionError,
    resolve_project_context,
    resolve_project_group,
)
from .registry import ExecutionRootError, ProjectRegistry, load_registry
from .session_adoption_policy import validate_adoption_mode
from .session_adoption_state import CodexSessionOrigins
from .session_connect import ConnectCandidate, SessionConnectStore
from .state import HubState, ProviderJobRecord
from .supervisor import CodexAppServerSupervisor
from .telegram_interaction import (
    telegram_contract_version,
    telegram_developer_instructions,
)
from .worker_execution import (
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


class ProviderTurnStopped(RuntimeError):
    def __init__(self, request_id: str) -> None:
        super().__init__("provider turn stopped by user")
        self.request_id = request_id


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
                model_provider=config.codex_model_provider,
                shared_socket_health=(
                    (lambda: probe_codex_runtime_proxy().ok)
                    if config.codex_multi_auth_dir is not None
                    else None
                ),
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
        self.state.recover_stale_provider_jobs(agent_id=self.agent.agent_id)
        if self._stop.is_set():
            return False
        job = self.state.lease_provider_job(
            self.agent.agent_id,
            self.worker_id,
            max_parallel_roots=self.config.max_parallel_roots,
            scheduler_agents=self.config.external_worker_agent_ids,
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

    def _run_connect_cycle(self) -> bool:
        """Handle one bounded metadata-only request after productive work."""
        store = SessionConnectStore(self.state)
        workflow = store.lease_worker(self.worker_id)
        if workflow is None:
            return False
        try:
            if workflow.canonical_root is None:
                raise ExternalQueueWorkerError("connect project is missing")
            if workflow.project_id is None:
                raise ExternalQueueWorkerError("connect project is missing")
            resolved = (
                resolve_project_context(
                    self.config,
                    self.state,
                    chat_id=workflow.destination_chat_id,
                    expected_project_id=workflow.project_id,
                    expected_root=workflow.canonical_root,
                )
                if workflow.destination_chat_id is not None
                else resolve_project_group(
                    self.config,
                    self.state,
                    project_id=workflow.project_id,
                    expected_root=workflow.canonical_root,
                )
            )
            self.registry = resolved.registry
            client = self._client()
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
            self._discard_client()
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
        executing = self.state.mark_provider_job_executing(job.job_id, lease_token)
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
            try:
                if isinstance(exc, ExecutionRootError):
                    self._last_error_code = exc.code
                    self._provider_state = "unavailable"
                    self.state.terminate_provider_job_with_notice(
                        executing.job_id,
                        token,
                        status="failed",
                        error_class="pre_execution",
                        error_code=exc.code,
                        sender_agent_id=self.agent.agent_id,
                        telegram_html=exc.public_message,
                    )
                elif isinstance(exc, ProviderTurnStopped):
                    self.state.cancel_active_provider_job(
                        executing.job_id, token, error_code="emergency_stop"
                    )
                    self.state.complete_emergency_stop(exc.request_id)
                    self._last_error_code = None
                    self._provider_state = "ready"
                    self._record_event("info", "provider_turn_stopped", self.agent.agent_id)
                elif isinstance(exc, ProviderLimitError):
                    self._last_error_code = "provider_limit"
                    self._provider_state = "limited"
                    self._quota_remaining_percent = float(exc.limit.remaining_percent)
                    self._quota_reset_at = datetime.fromtimestamp(exc.limit.resets_at, timezone.utc)
                    self.state.terminate_provider_job_with_notice(
                        executing.job_id,
                        token,
                        status="failed",
                        error_class="quota",
                        error_code=type(exc).__name__,
                        sender_agent_id=self.agent.agent_id,
                        telegram_html=(
                            f"{self.agent.display_name} limit reached. Reset telemetry was "
                            "recorded; use /accounts for the current status."
                        ),
                    )
                    self._record_event("warning", "provider_limit", exc.limit.to_json())
                elif isinstance(exc, ProviderUnavailableError):
                    self._last_error_code = exc.code
                    self._provider_state = "unavailable"
                    self.state.terminate_provider_job_with_notice(
                        executing.job_id,
                        token,
                        status="failed",
                        error_class="provider_unavailable",
                        error_code=exc.code,
                        sender_agent_id=self.agent.agent_id,
                        telegram_html=exc.public_message,
                    )
                    self._record_event("warning", "provider_unavailable", exc.code)
                else:
                    failure_class = (
                        "pre_execution"
                        if isinstance(exc, (CodexPreparationError, IncomingMaterialError))
                        else "ambiguous_execution"
                    )
                    recovered = False
                    if self.agent.runtime == "codex" and not isinstance(
                        exc, (CodexPreparationError, IncomingMaterialError)
                    ):
                        assert self.supervisor is not None
                        try:
                            recovered = reconcile_codex_completion(
                                self.state,
                                self.config,
                                project_root=Path(project.root),
                                job_id=executing.job_id,
                                lease_token=token,
                                agent_id=self.agent.agent_id,
                                client_factory=self.supervisor.client,
                            )
                        except Exception:
                            recovered = False
                    if recovered:
                        self._last_success_at = datetime.now(timezone.utc)
                        self._last_error_code = None
                        self._provider_state = "ready"
                        self._record_event("info", "provider_result_recovered", self.agent.agent_id)
                    else:
                        self._last_error_code = type(exc).__name__[:128]
                        self._provider_state = "unavailable"
                        # Keep the provider's bounded diagnostic in the private state DB.
                        # Without it every app-server protocol or quota failure collapses
                        # to an unhelpful ``RpcError`` and cannot be repaired remotely.
                        error_detail = " ".join(str(exc).split())[:1000] or None
                        # Invocation has been marked executing; no automatic replay
                        # is safe without runtime-specific proof that it never began.
                        self.state.terminate_provider_job_with_notice(
                            executing.job_id,
                            token,
                            status=(
                                "failed"
                                if isinstance(exc, (CodexPreparationError, IncomingMaterialError))
                                else "indeterminate"
                            ),
                            error_class=failure_class,
                            error_code=type(exc).__name__,
                            error_detail=error_detail,
                            sender_agent_id=self.agent.agent_id,
                            telegram_html=(
                                "Incoming material integrity validation failed; "
                                "the provider was not started. Send the material again."
                                if isinstance(exc, IncomingMaterialError)
                                else checkpoint_failure_notice(self.state, executing.job_id, exc)
                                if self.agent.runtime == "codex"
                                else uncertain_provider_notice(self.agent.display_name)
                            ),
                        )
                        self._record_event(
                            "warning",
                            "queued_provider_error",
                            f"{failure_class}:{type(exc).__name__}",
                        )
            except Exception:
                pass
            if self.agent.runtime == "codex":
                self._discard_client()
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=2)
            self._cleanup_incoming_material_staging(Path(project.root), executing.job_id)

    def _commit(
        self,
        job: ProviderJobRecord,
        token: str,
        *,
        visible_response: str,
        provider_session_id: str | None,
        actual_model: str | None,
        telegram_html: str,
        artifacts: tuple[ValidatedArtifact, ...] = (),
    ) -> None:
        try:
            self.state.commit_provider_result(
                job.job_id,
                token,
                visible_response=visible_response,
                sender_agent_id=self.agent.agent_id,
                telegram_html=telegram_html,
                provider_session_id=provider_session_id,
                actual_model=actual_model,
                user_excerpt=job.payload_text,
                acknowledge_context=job.context_watermark is not None,
                acknowledge_handoff=job.handoff_id is not None,
                telegram_contract_version=telegram_contract_version(self.agent.runtime),
                artifacts=artifacts,
            )
        except BaseException:
            spool_root = artifact_spool_root(self.config.state_path)
            for artifact in artifacts:
                try:
                    remove_spooled_artifact(artifact.path, spool_root)
                except Exception:
                    pass
            raise

    def _cleanup_artifact_staging(self, project_root: Path, job_id: str) -> None:
        try:
            cleanup_job_staging(project_root, job_id)
        except Exception as exc:
            self._record_event("warning", "artifact_staging_cleanup_error", type(exc).__name__)

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
            ensure_socket_health = getattr(self.supervisor, "ensure_shared_socket_health", None)
            if callable(ensure_socket_health) and not ensure_socket_health():
                self._discard_client()
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
        turn_id = start_codex_provider_turn(
            client,
            job,
            thread,
            project,
            prompt=codex_provider_prompt(turn_text, staging_dir=staging_dir),
            local_image_paths=prepared.local_image_paths,
        )
        journal.record_turn(job.job_id, token, turn_id)
        client.on_visible_item = lambda item_id, text, phase: journal.record_item(
            job.job_id, token, item_id, text, phase
        )
        client.on_completed = lambda result: journal.record_completion(
            job.job_id, token, result.text
        )
        monitor_stop = threading.Event()
        interrupted_request: list[str] = []

        def monitor_control() -> None:
            monitor_state = HubState.open(self.config.state_path)
            try:
                while not monitor_stop.wait(0.2):
                    request_id = monitor_state.pending_emergency_stop(
                        job.topic_id, self.agent.agent_id
                    )
                    if request_id is not None:
                        try:
                            assert self.supervisor is not None
                            if self.supervisor.transport_mode == "stdio-fallback":
                                client.close()
                            else:
                                interrupt_client = self.supervisor.client()
                                try:
                                    interrupt_client.interrupt_turn(
                                        thread_id=thread.thread_id, turn_id=turn_id
                                    )
                                finally:
                                    interrupt_client.close()
                        finally:
                            interrupted_request.append(request_id)
                        return
                    assert self.supervisor is not None
                    if self.supervisor.transport_mode == "stdio-fallback":
                        # A fallback client owns a private app-server process;
                        # a second client cannot address its active turn.
                        continue
                    followup = monitor_state.lease_steer_followup(
                        job.job_id, f"{self.worker_id}-steer", lease_seconds=120
                    )
                    if followup is None or followup.lease_token is None:
                        continue
                    steer_token = followup.lease_token
                    monitor_state.mark_provider_job_executing(followup.job_id, steer_token)
                    steer_client = None
                    try:
                        steer_client = self.supervisor.client()
                        returned_turn = steer_client.steer_turn(
                            thread_id=thread.thread_id,
                            turn_id=turn_id,
                            text=followup.payload_text,
                            client_user_message_id=followup.job_id,
                        )
                        monitor_state.complete_steered_job(
                            followup.job_id,
                            steer_token,
                            parent_job_id=job.job_id,
                            provider_turn_id=returned_turn,
                        )
                    except RpcRejectedError:
                        monitor_state.reject_unaccepted_steer(followup.job_id, steer_token)
                    except Exception as exc:
                        monitor_state.mark_provider_job_indeterminate(
                            followup.job_id,
                            steer_token,
                            error_code=type(exc).__name__,
                            error_detail="same-turn steering outcome is unknown",
                        )
                    finally:
                        if steer_client is not None:
                            try:
                                steer_client.close()
                            except Exception:
                                pass
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
        except Exception:
            pending_request = self.state.pending_emergency_stop(job.topic_id, self.agent.agent_id)
            if interrupted_request or pending_request is not None:
                request_id = interrupted_request[0] if interrupted_request else pending_request
                assert request_id is not None
                raise ProviderTurnStopped(request_id) from None
            raise
        finally:
            client.on_visible_item = None
            client.on_completed = None
            monitor_stop.set()
            monitor.join(timeout=2)
        late_request = self.state.pending_emergency_stop(job.topic_id, self.agent.agent_id)
        if interrupted_request:
            raise ProviderTurnStopped(interrupted_request[0])
        if late_request is not None:
            raise ProviderTurnStopped(late_request)
        try:
            self.state.set_context_remaining(job.session_id, context_remaining_percent(result))
        except Exception:
            pass
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
        self._commit(
            job,
            token,
            visible_response=prepared_result.visible_response,
            provider_session_id=thread.thread_id,
            actual_model=thread.model,
            telegram_html=prepared_result.telegram_html,
            artifacts=artifacts.artifacts,
        )
        cleanup_consumed_raw_inputs(prepared)
        cleanup_materialized_inputs(prepared)
        self._cleanup_artifact_staging(Path(project.root), job.job_id)

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
                    request_id = monitor_state.pending_emergency_stop(
                        job.topic_id, self.agent.agent_id
                    )
                    if request_id is None:
                        continue
                    interrupted_request.append(request_id)
                    adapter.interrupt()
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
            )
        except ExternalTurnInterrupted:
            if interrupted_request:
                raise ProviderTurnStopped(interrupted_request[0]) from None
            raise
        finally:
            monitor_stop.set()
            monitor.join(timeout=2)
        late_request = self.state.pending_emergency_stop(job.topic_id, self.agent.agent_id)
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
        self._commit(
            job,
            token,
            visible_response=prepared_result.visible_response,
            provider_session_id=result.provider_session_id,
            actual_model=actual_model,
            telegram_html=prepared_result.telegram_html,
            artifacts=artifacts.artifacts,
        )
        cleanup_consumed_raw_inputs(prepared)
        cleanup_materialized_inputs(prepared)
        self._cleanup_artifact_staging(Path(project.root), job.job_id)
