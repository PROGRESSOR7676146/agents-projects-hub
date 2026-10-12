"""Bounded late owner-stop service, separate from productive and connect workers."""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from .codex_control_authority import begin_codex_ingress_or_stop, begin_codex_interrupt
from .codex_control_connection import ControlSendPath
from .codex_control_recovery import RecoveryControlClient, observe_after_control_loss
from .codex_ingress_notice import prepare_ingress_notice
from .codex_observed_result import ObservedTurnResults
from .codex_turn_controls import ActiveTurnProof
from .diagnostic_log import survived
from .hub_config import HubConfig
from .project_resolution import resolve_project_context
from .state import HubState, StateError
from .topic_execution import resolve_topic_execution_root


def run_late_control_once(
    state: HubState,
    config: HubConfig,
    *,
    worker_id: str,
    agent_id: str,
    client_factory: Callable[[float], RecoveryControlClient],
    now: datetime | None = None,
    on_control_path: Callable[[ControlSendPath | None], None] | None = None,
) -> bool:
    target = state.codex_controls.claim_late_read(worker_id, agent_id=agent_id, now=now)
    if target is None:
        return False
    return _run_claimed_control(
        state,
        config,
        target=target,
        ingress=False,
        agent_id=agent_id,
        client_factory=client_factory,
        on_control_path=on_control_path,
    )


def _run_claimed_control(
    state: HubState,
    config: HubConfig,
    *,
    target: sqlite3.Row,
    ingress: bool,
    agent_id: str,
    client_factory: Callable[[float], RecoveryControlClient],
    on_control_path: Callable[[ControlSendPath | None], None] | None = None,
) -> bool:
    """One claimed-target lifecycle for real stops and ingress observation."""
    job_id = str(target["job_id"])
    claim = str(target["read_claim_token"])
    root = Path(target["project_root"])
    client = None
    path = None

    def begin(proof: ActiveTurnProof, deadline: float) -> str | None:
        if ingress:
            reservation = begin_codex_ingress_or_stop(
                state,
                config,
                job_id=job_id,
                proof=proof,
                deadline=deadline,
                read_claim_token=claim,
            )
            return None if reservation is None else reservation.owner
        return begin_codex_interrupt(
            state,
            config,
            job_id=job_id,
            source="late",
            proof=proof,
            deadline=deadline,
            read_claim_token=claim,
        )

    try:
        topic = state.get_topic(int(target["topic_id"]))
        current = resolve_project_context(
            config, state, chat_id=topic.chat_id, expected_project_id=topic.project_id
        )
        if resolve_topic_execution_root(state, current.registry, topic) != root:
            raise StateError("late control project binding changed")
        # The owner injects a fresh owning-server factory with fallback disabled.
        client = client_factory(time.monotonic() + 2)
        owned_client = client
        path = ControlSendPath(lambda: _close_client(owned_client))
        if on_control_path is not None:
            on_control_path(path)
        outcome = observe_after_control_loss(
            client,
            thread_id=target["provider_thread_id"],
            turn_id=target["provider_turn_id"],
            root=root,
            send_scope=path.sending_scope,
            begin_interrupt=begin,
            finish_interrupt=lambda owner, result: state.codex_controls.finish_interrupt(
                job_id,
                owner,
                outcome=result,
                send_path_quiesced=result != "unknown",
            ),
        )
        ObservedTurnResults(state, config).apply_outcome(
            job_id, target["provider_thread_id"], target["provider_turn_id"], root, outcome
        )
        state.record_runtime_event(agent_id, "info", "codex_late_control_" + outcome.status, job_id)
    except Exception as error:
        survived("codex_late_control.unconfirmed", error)
    finally:
        if path is not None:
            path.request_close()
        elif client is not None:
            _close_client(client)
        if on_control_path is not None:
            on_control_path(None)
        if ingress:
            prepare_ingress_notice(state, job_id)
        state.codex_controls.finish_late_read(job_id, claim)
    return True


class CodexIngressSweep:
    """Fair bounded keyset scan, sharing the existing maintenance thread."""

    def __init__(self) -> None:
        self.after: str | None = None
        self.through: str | None = None
        self.next_sweep = 0.0

    def run_once(
        self,
        state: HubState,
        config: HubConfig,
        *,
        worker_id: str,
        agent_id: str,
        client_factory: Callable[[float], RecoveryControlClient],
        on_control_path: Callable[[ControlSendPath | None], None] | None = None,
        stopped: Callable[[], bool] | None = None,
    ) -> bool:
        if (stopped is not None and stopped()) or time.monotonic() < self.next_sweep:
            return False
        ids, self.through = state.codex_ingress_control.candidate_page(
            agent_id, after=self.after, through=self.through
        )
        for job_id in ids:
            if stopped is not None and stopped():
                return False
            self.after = job_id
            try:
                target = state.codex_ingress_control.claim_read(job_id, worker_id)
            except Exception as error:
                survived("codex_ingress_control.claim_unconfirmed", error)
                continue
            if target is not None:
                return _run_claimed_control(
                    state,
                    config,
                    target=target,
                    ingress=True,
                    agent_id=agent_id,
                    client_factory=client_factory,
                    on_control_path=on_control_path,
                )
        if len(ids) < 32 or self.after == self.through:
            self.after = self.through = None
            self.next_sweep = time.monotonic() + 30
        return False


def _close_client(client: RecoveryControlClient) -> None:
    try:
        client.close()
    except Exception as error:
        survived("codex_late_control.client_close", error)


class CodexControlMaintenance:
    """One small worker-owned thread with its own state connection and clients."""

    def __init__(
        self,
        config: HubConfig,
        *,
        worker_id: str,
        agent_id: str,
        client_factory: Callable[[float], RecoveryControlClient],
        stop: threading.Event,
    ) -> None:
        self.config = config
        self.worker_id = worker_id
        self.agent_id = agent_id
        self.client_factory = client_factory
        self.stop = stop
        self.lock = threading.Lock()
        self.control_path: ControlSendPath | None = None
        self.ingress = CodexIngressSweep()

    def run_once(self, state: HubState) -> bool:
        def ingress() -> bool:
            try:
                return self.ingress.run_once(
                    state,
                    self.config,
                    worker_id=self.worker_id,
                    agent_id=self.agent_id,
                    client_factory=self.client_factory,
                    on_control_path=self._set_control_path,
                    stopped=self.stop.is_set,
                )
            except Exception as error:
                survived("codex_ingress_control.cycle_unconfirmed", error)
                return False

        worked = run_late_control_once(
            state,
            self.config,
            worker_id=self.worker_id,
            agent_id=self.agent_id,
            client_factory=self.client_factory,
            on_control_path=self._set_control_path,
        )
        if self.stop.is_set():
            return worked
        # One real-stop cycle first and at most one ingress cycle per pass.
        # Both categories make progress without an unbounded stop drain or a
        # new scheduler, and a same-target stop still excludes ingress claims.
        return ingress() or worked

    def _set_control_path(self, path: ControlSendPath | None) -> None:
        with self.lock:
            self.control_path = path
        if path is not None and self.stop.is_set():
            path.request_close()

    def close_client(self) -> None:
        with self.lock:
            path = self.control_path
        if path is not None:
            path.request_close()

    def run_forever(self) -> None:
        while not self.stop.is_set():
            try:
                state = HubState.open_existing(
                    self.config.state_path,
                    codex_permission_profile=self.config.codex_permission_profile,
                    contention_timeout_seconds=0.1,
                )
                try:
                    while not self.stop.is_set():
                        worked = self.run_once(state)
                        self.stop.wait(0.01 if worked else 0.2)
                finally:
                    state.close()
            except Exception as error:
                survived("codex_late_control.cycle", error)
                self.stop.wait(1)
