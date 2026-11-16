"""Settle Codex executions interrupted by a reboot before the first server start.

ADR 0104: only the receipt-less first start attempt of a boot runs this, after
its own native-client and server refusal and before the unchanged idle gate.
It reuses the ordinary model-free worker reconciliation: exact read-only turn
observation through the already verified pinned binary over stdio, then a
confirmed result or an indeterminate notice. It never replays, resumes or
retries a turn, and it never touches queued work. Whatever it cannot settle
within its deadline stays in place, so the idle gate refuses exactly as before.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

from .codex_appserver import CodexAppServerClient
from .codex_recovery import recover_codex_job
from .codex_transports import StdioJsonLineTransport
from .hub_config import load_hub_config
from .registry import load_registry
from .state import HubState, StateError

AGENT_ID = "codex"
WORKER_ID = "codex-boot-recovery"
# A crashed worker's lease lasts 120 s; the server readiness budget is 270 s,
# so the whole reconciliation, cleanup included, ends within 150 s.
DEFAULT_DEADLINE_SECONDS = 150.0
# The temporary client is closed after its reads: terminate, then reap.
CLEANUP_SECONDS = 10.0
# A job is claimed only with time left for a full handshake and exact read,
# so the deadline never turns a confirmable result into an indeterminate one.
CLAIM_RESERVE_SECONDS = 40.0
MAX_RECOVERIES = 64
INITIALIZE_SECONDS = 20.0


def _same_file(first: Path, second: Path) -> bool:
    left, right = os.stat(first), os.stat(second)
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def stdio_client_factory(
    binary: Path,
    *,
    model_provider: str | None,
    permission_profile: str | None,
    initialize_seconds: float = INITIALIZE_SECONDS,
    deadline: float | None = None,
) -> Callable[[], CodexAppServerClient]:
    """Return a factory for one-shot stdio clients of the verified pinned binary.

    Each exact turn read has its own short RPC deadline; the handshake gets one
    here, so a hung child cannot hold the start beyond the readiness budget.
    """

    def factory() -> CodexAppServerClient:
        transport = StdioJsonLineTransport.start(str(binary))
        try:
            client = CodexAppServerClient(
                transport,
                approval_policy="never",
                model_provider=model_provider,
                permission_profile=permission_profile,
                transport_mode="stdio-fallback",
            )
            bound = time.monotonic() + initialize_seconds
            client.initialize(deadline=bound if deadline is None else min(bound, deadline))
        except BaseException:
            transport.close()
            raise
        return client

    return factory


def _executing_jobs(state: HubState) -> int:
    row = state._connection.execute(
        "SELECT count(*) FROM provider_jobs WHERE agent_id=? AND status='executing'",
        (AGENT_ID,),
    ).fetchone()
    return int(row[0])


def reconcile_previous_boot(
    manifest: dict,
    hub_config: Path,
    *,
    verified_binary: Path,
    deadline_seconds: float | None = None,
    client_factory: Callable[[], CodexAppServerClient] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Reconcile interrupted Codex executions; return how many were settled.

    Waits, within one monotonic deadline, for leases left by the crashed boot
    to expire, because the ordinary claim only takes an expired lease. A lease
    that stays valid, for example after a wall-clock jump, is left in place, and
    so is a job that expires too late for a full exact read. Every client call
    of a claimed job ends before the deadline minus the cleanup reserve.
    """
    config = load_hub_config(hub_config)
    if not _same_file(config.state_path, Path(manifest["state_path"])):
        raise StateError("Hub configuration names a different state database")
    os.environ["CODEX_HOME"] = manifest["codex_home"]
    registry = load_registry(config.registry_path)
    budget = DEFAULT_DEADLINE_SECONDS if deadline_seconds is None else deadline_seconds
    deadline = monotonic() + budget
    rpc_deadline = deadline - CLEANUP_SECONDS
    claim_deadline = rpc_deadline - CLAIM_RESERVE_SECONDS
    factory = client_factory or stdio_client_factory(
        verified_binary,
        model_provider=config.codex_model_provider,
        permission_profile=config.codex_permission_profile,
        deadline=rpc_deadline,
    )
    settled = 0
    state = HubState.open_existing(
        config.state_path, codex_permission_profile=config.codex_permission_profile
    )
    try:
        while settled < MAX_RECOVERIES and monotonic() < claim_deadline:
            if recover_codex_job(
                state, config, registry, AGENT_ID, WORKER_ID, factory, deadline=rpc_deadline
            ):
                settled += 1
                continue
            if not _executing_jobs(state):
                break
            sleep(min(1.0, max(0.0, claim_deadline - monotonic())))
    finally:
        state.close()
    return settled
