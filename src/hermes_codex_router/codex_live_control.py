"""One accepted turn's stop/steer observer, with no productive RPC replay."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

from .codex_appserver import RpcRejectedError, StoredTurnOutcome
from .codex_control_authority import begin_codex_ingress_or_stop, begin_codex_interrupt
from .codex_control_connection import ControlSendPath
from .codex_control_recovery import observe_after_control_loss
from .codex_ingress_notice import prepare_ingress_notice
from .codex_turn_controls import ActiveTurnProof
from .diagnostic_log import survived
from .execution_journal import ExecutionJournal
from .hub_config import HubConfig
from .sqlite_contention import is_sqlite_contention
from .state import HubState, ProviderJobRecord


class ControlClient(Protocol):
    def read_turn_outcome(
        self, *, thread_id: str, turn_id: str, cwd: Path, deadline: float | None = None
    ) -> StoredTurnOutcome: ...
    def interrupt_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        deadline: float | None = None,
        send_start_deadline: float | None = None,
    ) -> None: ...
    def steer_turn(
        self, *, thread_id: str, turn_id: str, text: str, client_user_message_id: str
    ) -> str: ...
    def close(self) -> None: ...


class CodexLiveControlError(RuntimeError):
    pass


def _fence_ends_observation(target: sqlite3.Row | None, *, ingress: bool) -> bool:
    return (
        target is not None
        and target["send_started_at"] is not None
        and not (
            ingress
            and target["interrupt_outcome"] == "not_sent"
            and target["owner_quiesced_at"] is not None
        )
    )


@dataclass(frozen=True)
class PendingStateOperation:
    child: ProviderJobRecord
    operation: Literal["start", "release", "reject", "complete", "unknown"]
    returned_turn: str | None = None
    error: Exception | None = None


class CodexLiveControl:
    def __init__(
        self,
        *,
        state_factory: Callable[[], HubState],
        config: HubConfig,
        client_factory: Callable[[], ControlClient],
        job: ProviderJobRecord,
        worker_id: str,
        thread_id: str,
        turn_id: str,
        transport_mode: str | None,
        close_owned_turn_client: Callable[[], None],
        poll_seconds: float = 0.2,
        stop_retry_seconds: float = 5,
    ) -> None:
        self.state_factory = state_factory
        self.config = config
        self.client_factory = client_factory
        self.job = job
        self.worker_id = worker_id
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.transport_mode = transport_mode
        self.close_owned_turn_client = close_owned_turn_client
        self.poll_seconds = poll_seconds
        self.stop_retry_seconds = stop_retry_seconds
        self.confirmed_interrupt_request: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client_lock = threading.Lock()
        self._active_client: ControlClient | None = None
        self._active_send_path: ControlSendPath | None = None
        self._failure: BaseException | None = None
        self._contention_episode = False
        self._steering_rejected = False
        self._pending: PendingStateOperation | None = None
        self._deferred_failure: Exception | None = None
        self._stop_no_send_attempts = 0
        self._next_stop_attempt = 0.0
        self._next_ingress_assessment = 0.0
        self._next_ingress_observation = 0.0

    def start(self) -> None:
        if self._thread is not None:
            raise CodexLiveControlError("control observer already started")
        self._thread = threading.Thread(target=self._run, name="codex-live-control", daemon=True)
        self._thread.start()

    @contextmanager
    def running(self) -> Iterator[None]:
        self.start()
        try:
            yield
        except BaseException:
            try:
                self.stop_and_join()
            except BaseException as error:
                survived("codex_live_control.shutdown", error)
            raise
        else:
            self.stop_and_join()

    def stop_and_join(self, *, timeout: float = 20) -> None:
        self._stop.set()
        with self._client_lock:
            client, self._active_client = self._active_client, None
            path, self._active_send_path = self._active_send_path, None
        if path is not None:
            path.request_close()
        elif client is not None:
            self._close_client(client)
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise CodexLiveControlError("control observer shutdown is unconfirmed")
        if self._failure is not None:
            raise CodexLiveControlError("control observer failed") from self._failure

    def raise_deferred_failure(self) -> None:
        """Called by the worker only after evaluating stop provenance."""
        if self._deferred_failure is not None:
            raise CodexLiveControlError("steering observer failed") from self._deferred_failure

    def _defer_steering_failure(self, error: Exception) -> None:
        self._steering_rejected = True
        if self._deferred_failure is None:
            self._deferred_failure = error
            survived("codex_live_control.steering_failure", error)

    @staticmethod
    def _close_client(client: ControlClient) -> None:
        try:
            client.close()
        except Exception as error:
            survived("codex_live_control.client_close", error)

    def _acquire_client(self) -> ControlClient | None:
        if self._stop.is_set():
            return None
        client = self.client_factory()
        with self._client_lock:
            stopped = self._stop.is_set()
            if not stopped:
                self._active_client = client
                self._active_send_path = ControlSendPath(lambda: self._close_client(client))
        if stopped:
            self._close_client(client)
            return None
        return client

    def _release_client(self, client: ControlClient) -> None:
        with self._client_lock:
            owned = self._active_client is client
            if owned:
                self._active_client = None
                path, self._active_send_path = self._active_send_path, None
            else:
                path = None
        if owned:
            if path is not None:
                path.request_close()
            else:
                self._close_client(client)

    def _observe_contention(self, error: BaseException) -> None:
        if not self._contention_episode:
            survived("codex_live_control.contention", error)
            self._contention_episode = True

    def _run(self) -> None:
        state: HubState | None = None
        try:
            while not self._stop.wait(self.poll_seconds):
                try:
                    if state is None:
                        state = self.state_factory()
                    if self._stop.is_set():
                        return
                    request_id = state.pending_emergency_stop_for_job(self.job.job_id)
                    if request_id is not None:
                        if time.monotonic() < self._next_stop_attempt:
                            continue
                        if self._interrupt(state, request_id):
                            return
                        self._stop_no_send_attempts += 1
                        if self._stop_no_send_attempts >= 3:
                            return
                        self._next_stop_attempt = time.monotonic() + self.stop_retry_seconds
                        continue
                    if self._poll_ingress(state):
                        return
                    if self.transport_mode != "stdio-fallback":
                        try:
                            self._steer(state)
                        except Exception as error:
                            if is_sqlite_contention(error):
                                raise
                            self._defer_steering_failure(error)
                    self._contention_episode = False
                except Exception as error:
                    if not is_sqlite_contention(error):
                        raise
                    # Retry the next state poll, never an earlier provider call.
                    self._observe_contention(error)
        except BaseException as error:
            self._failure = error
            survived("codex_live_control.failure", error)
            # The worker may otherwise remain blocked for hours waiting for
            # the native result after its independent control observer died.
            # Wake only this job's owned client; worker recovery determines
            # the exact native outcome and may target that accepted turn.
            try:
                self.close_owned_turn_client()
            except Exception as close_error:
                survived("codex_live_control.turn_client_close", close_error)
        finally:
            if state is not None:
                try:
                    # Exactly one final state-only attempt; never start or invoke
                    # a provider operation while the observer is shutting down.
                    self._settle_pending(state, stopping=True)
                except Exception as error:
                    if is_sqlite_contention(error):
                        self._observe_contention(error)
                    else:
                        self._defer_steering_failure(error)
                try:
                    state.close()
                except Exception as error:
                    survived("codex_live_control.state_close", error)

    def _interrupt(self, state: HubState, request_id: str) -> bool:
        """End retries on a send fence/terminal proof; keep no-send streams alive."""
        return self._observe_control(state, request_id=request_id, ingress=False)

    def _poll_ingress(self, state: HubState) -> bool:
        """Optional assessment/control faults must never kill the primary stream."""
        try:
            if self._stop.is_set() or self.transport_mode not in {"socket", "managed-socket"}:
                return False
            current = time.monotonic()
            if current < self._next_ingress_assessment:
                return False
            self._next_ingress_assessment = current + 5
            if state.telegram_turn_provenance.target(self.job.job_id) is None:
                return False
            assessment = state.telegram_ingress_assessments.assess(
                self.job.job_id, now=datetime.now(timezone.utc)
            )
            if assessment.episode is None or assessment.episode.deadline > datetime.now(
                timezone.utc
            ):
                return False
            if current < self._next_ingress_observation:
                return False
            # Advance before acquisition. Failures or ingress flapping cannot
            # open a new native observation window sooner than thirty seconds.
            self._next_ingress_observation = current + 30
            return self._observe_control(state, request_id=None, ingress=True)
        except Exception as error:
            survived("codex_live_control.ingress_unconfirmed", error)
            return False

    def _observe_control(self, state: HubState, *, request_id: str | None, ingress: bool) -> bool:
        """One lifecycle for exact owner stops and optional ingress precautions."""
        client = None
        wake_primary = False
        fenced = False
        selected_stop = request_id

        def begin(proof: ActiveTurnProof, deadline: float) -> str | None:
            nonlocal selected_stop
            if ingress:
                reservation = begin_codex_ingress_or_stop(
                    state,
                    self.config,
                    job_id=self.job.job_id,
                    proof=proof,
                    deadline=deadline,
                    invocation_token=self.job.lease_token,
                )
                if reservation is None:
                    return None
                selected_stop = reservation.real_stop_request
                return reservation.owner
            return begin_codex_interrupt(
                state,
                self.config,
                job_id=self.job.job_id,
                source="live",
                proof=proof,
                deadline=deadline,
                invocation_token=self.job.lease_token,
            )

        def observe_event(event: str) -> None:
            nonlocal wake_primary
            if event == "attempted":
                wake_primary = True
            if ingress and state.codex_ingress_control.read_cause(self.job.job_id) is not None:
                state.record_runtime_event(
                    self.job.agent_id,
                    "warning",
                    "codex_ingress_interrupt_" + event,
                    self.job.job_id,
                )

        try:
            if self._stop.is_set():
                return True
            if self.transport_mode == "stdio-fallback":
                wake_primary = True
            else:
                target = state.codex_controls.read(self.job.job_id)
                if target is None or self.job.lease_token is None:
                    return True
                if target["send_started_at"] is not None:
                    return _fence_ends_observation(target, ingress=ingress)
                client = self._acquire_client()
                if client is None or self._stop.is_set():
                    return self._stop.is_set()
                with self._client_lock:
                    path = self._active_send_path if self._active_client is client else None
                if path is None:
                    return True
                outcome = observe_after_control_loss(
                    client,
                    thread_id=self.thread_id,
                    turn_id=self.turn_id,
                    root=Path(target["project_root"]),
                    send_scope=path.sending_scope,
                    begin_interrupt=begin,
                    finish_interrupt=lambda owner, result: state.codex_controls.finish_interrupt(
                        self.job.job_id,
                        owner,
                        outcome=result,
                        send_path_quiesced=result != "unknown",
                    ),
                    on_interrupt_event=observe_event,
                )
                if outcome.status in {"completed", "failed", "interrupted"}:
                    wake_primary = True
                if outcome.result is not None:
                    ExecutionJournal(state).record_completion(
                        self.job.job_id, self.job.lease_token, outcome.result.text
                    )
                target = state.codex_controls.read(self.job.job_id)
                fenced = _fence_ends_observation(target, ingress=ingress)
                if target is not None and target["interrupt_outcome"] == "matched_ack":
                    self.confirmed_interrupt_request = selected_stop
        except Exception as error:
            if is_sqlite_contention(error):
                # Pre-send contention keeps the productive stream intact.
                # A post-send completion write may also contend; the local
                # attempted flag still wakes recovery without repeating RPC.
                if ingress and wake_primary:
                    return True
                raise
            survived("codex_live_control.interrupt_unconfirmed", error)
        finally:
            if client is not None:
                self._release_client(client)
            if ingress:
                prepare_ingress_notice(state, self.job.job_id)
            # A sent interrupt must wake the primary even if native work ignores
            # ACK. Without a send, keep consuming progress and the saved final.
            if wake_primary:
                try:
                    self.close_owned_turn_client()
                except Exception as error:
                    survived("codex_live_control.turn_client_close", error)
        return wake_primary or fenced

    def _steer(self, state: HubState) -> None:
        if self._stop.is_set():
            return
        if self._pending is not None:
            ready = self._settle_pending(state)
            if ready is not None:
                self._invoke_steer(state, ready)
            return
        if self._steering_rejected:
            return
        child = state.lease_steer_followup(
            self.job.job_id, f"{self.worker_id}-steer", lease_seconds=120
        )
        if child is None or child.lease_token is None:
            return
        self._pending = PendingStateOperation(child, "start")
        ready = self._settle_pending(state, stopping=self._stop.is_set())
        if ready is not None:
            self._invoke_steer(state, ready)

    def _settle_pending(
        self, state: HubState, *, stopping: bool = False
    ) -> ProviderJobRecord | None:
        pending = self._pending
        if pending is None:
            return None
        if stopping and pending.operation == "start":
            pending = self._pending = PendingStateOperation(pending.child, "release")
        child = pending.child
        assert child.lease_token is not None
        try:
            if pending.operation == "start":
                started = state.start_steer_followup(
                    child.job_id, child.lease_token, parent_job_id=self.job.job_id
                )
                self._pending = None
                return child if started.status == "executing" else None
            if pending.operation == "release":
                state.release_provider_job_lease(child.job_id, child.lease_token)
            elif pending.operation == "reject":
                state.reject_unaccepted_steer(child.job_id, child.lease_token)
            elif pending.operation == "complete":
                assert pending.returned_turn is not None
                state.complete_steered_job(
                    child.job_id,
                    child.lease_token,
                    parent_job_id=self.job.job_id,
                    provider_turn_id=pending.returned_turn,
                )
            else:
                assert pending.error is not None
                state.mark_provider_job_indeterminate(
                    child.job_id,
                    child.lease_token,
                    error_code=type(pending.error).__name__,
                    error_detail=(
                        "same-turn steering was accepted; settlement failed for turn "
                        + pending.returned_turn[:256]
                        if pending.returned_turn is not None
                        else "same-turn steering outcome is unknown"
                    ),
                )
        except Exception as error:
            if is_sqlite_contention(error):
                # Retain the exact state write, not a callback that could invoke.
                raise
            self._pending = None
            if pending.operation == "complete":
                self._defer_steering_failure(error)
                self._pending = PendingStateOperation(
                    child, "unknown", pending.returned_turn, error
                )
                # The next poll checks stop before attempting uncertainty
                # marking. Keep this first failure as the deferred cause.
            raise
        self._pending = None
        return None

    def _invoke_steer(self, state: HubState, child: ProviderJobRecord) -> None:
        assert child.lease_token is not None
        client = None
        rpc_attempted = False
        try:
            try:
                client = self._acquire_client()
                if client is None or self._stop.is_set():
                    self._pending = PendingStateOperation(child, "reject")
                else:
                    rpc_attempted = True
                    returned_turn = client.steer_turn(
                        thread_id=self.thread_id,
                        turn_id=self.turn_id,
                        text=child.payload_text,
                        client_user_message_id=child.job_id,
                    )
                    self._pending = PendingStateOperation(child, "complete", returned_turn)
            except RpcRejectedError as error:
                # Return the FIFO head to ordinary execution; later polls must
                # not send it (or a later child) into this rejected parent again.
                self._steering_rejected = True
                if not rpc_attempted:
                    self._defer_steering_failure(error)
                self._pending = PendingStateOperation(child, "reject")
            except Exception as error:
                self._steering_rejected = True
                if not rpc_attempted:
                    self._defer_steering_failure(error)
                self._pending = PendingStateOperation(
                    child,
                    "unknown" if rpc_attempted else "reject",
                    error=error,
                )
            self._settle_pending(state)
        finally:
            if client is not None:
                self._release_client(client)
