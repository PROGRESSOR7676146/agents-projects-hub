"""One accepted turn's stop/steer observer, with no productive RPC replay."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Protocol

from .codex_appserver import RpcRejectedError
from .diagnostic_log import survived
from .sqlite_contention import is_sqlite_contention
from .state import HubState, ProviderJobRecord


class ControlClient(Protocol):
    def interrupt_turn(self, *, thread_id: str, turn_id: str) -> None: ...
    def steer_turn(
        self, *, thread_id: str, turn_id: str, text: str, client_user_message_id: str
    ) -> str: ...
    def close(self) -> None: ...


class CodexLiveControlError(RuntimeError):
    pass


class CodexLiveControl:
    def __init__(
        self,
        *,
        state_factory: Callable[[], HubState],
        client_factory: Callable[[], ControlClient],
        job: ProviderJobRecord,
        worker_id: str,
        thread_id: str,
        turn_id: str,
        transport_mode: str | None,
        close_owned_turn_client: Callable[[], None],
        poll_seconds: float = 0.2,
    ) -> None:
        self.state_factory = state_factory
        self.client_factory = client_factory
        self.job = job
        self.worker_id = worker_id
        self.thread_id = thread_id
        self.turn_id = turn_id
        self.transport_mode = transport_mode
        self.close_owned_turn_client = close_owned_turn_client
        self.poll_seconds = poll_seconds
        self.confirmed_interrupt_request: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._client_lock = threading.Lock()
        self._active_client: ControlClient | None = None
        self._failure: BaseException | None = None
        self._contention_episode = False
        self._steering_rejected = False

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

    def stop_and_join(self, *, timeout: float = 10) -> None:
        self._stop.set()
        with self._client_lock:
            client, self._active_client = self._active_client, None
        if client is not None:
            self._close_client(client)
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise CodexLiveControlError("control observer shutdown is unconfirmed")
        if self._failure is not None:
            raise CodexLiveControlError("control observer failed") from self._failure

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
        if stopped:
            self._close_client(client)
            return None
        return client

    def _release_client(self, client: ControlClient) -> None:
        with self._client_lock:
            owned = self._active_client is client
            if owned:
                self._active_client = None
        if owned:
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
                        self._interrupt(request_id)
                        return
                    if self.transport_mode != "stdio-fallback":
                        self._steer(state)
                    self._contention_episode = False
                except Exception as error:
                    if not is_sqlite_contention(error):
                        raise
                    # Retry the next state poll, never an earlier provider call.
                    self._observe_contention(error)
        except BaseException as error:
            self._failure = error
            survived("codex_live_control.failure", error)
        finally:
            if state is not None:
                try:
                    state.close()
                except Exception as error:
                    if self._failure is None:
                        self._failure = error

    def _interrupt(self, request_id: str) -> None:
        client = None
        try:
            if self._stop.is_set():
                return
            if self.transport_mode == "stdio-fallback":
                self.close_owned_turn_client()
            else:
                client = self._acquire_client()
                if client is None or self._stop.is_set():
                    return
                client.interrupt_turn(thread_id=self.thread_id, turn_id=self.turn_id)
        except Exception as error:
            survived("codex_live_control.interrupt_unconfirmed", error)
        else:
            self.confirmed_interrupt_request = request_id
        finally:
            if client is not None:
                self._release_client(client)

    def _unknown(self, state: HubState, child: ProviderJobRecord, error: Exception) -> None:
        assert child.lease_token is not None
        state.mark_provider_job_indeterminate(
            child.job_id,
            child.lease_token,
            error_code=type(error).__name__,
            error_detail="same-turn steering outcome is unknown",
        )

    def _steer(self, state: HubState) -> None:
        if self._stop.is_set() or self._steering_rejected:
            return
        child = state.lease_steer_followup(
            self.job.job_id, f"{self.worker_id}-steer", lease_seconds=120
        )
        if child is None or child.lease_token is None or self._stop.is_set():
            return
        started = state.start_steer_followup(
            child.job_id, child.lease_token, parent_job_id=self.job.job_id
        )
        if started.status != "executing" or self._stop.is_set():
            return
        client = None
        try:
            try:
                client = self._acquire_client()
                if client is None or self._stop.is_set():
                    return
                returned_turn = client.steer_turn(
                    thread_id=self.thread_id,
                    turn_id=self.turn_id,
                    text=child.payload_text,
                    client_user_message_id=child.job_id,
                )
            except RpcRejectedError:
                # Return the FIFO head to ordinary execution; later polls must
                # not send it (or a later child) into this rejected parent again.
                self._steering_rejected = True
                state.reject_unaccepted_steer(child.job_id, child.lease_token)
                return
            except Exception as error:
                self._unknown(state, child, error)
                return
            try:
                state.complete_steered_job(
                    child.job_id,
                    child.lease_token,
                    parent_job_id=self.job.job_id,
                    provider_turn_id=returned_turn,
                )
            except Exception as error:
                try:
                    self._unknown(state, child, error)
                except Exception as marking_error:
                    if not is_sqlite_contention(error):
                        raise error from marking_error
                    raise
                if not is_sqlite_contention(error):
                    raise
        finally:
            if client is not None:
                self._release_client(client)
