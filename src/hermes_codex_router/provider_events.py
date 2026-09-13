from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .codex_accounts import CodexAccountStatus, CodexPoolStatus
from .state import HubState


@dataclass(frozen=True, slots=True)
class CodexRuntimeSnapshot:
    rate_limited_responses: int
    account_rotations: int
    active_account_index: int | None


@dataclass(frozen=True, slots=True)
class CodexRotationObservation:
    source_account_index: int | None
    target_account_index: int | None
    provider_limit_count: int
    rotation_count: int


def detect_codex_rotation(
    snapshot: CodexRuntimeSnapshot,
    *,
    pool: CodexPoolStatus,
    previous_rate_limits: int | None,
    previous_rotations: int | None,
    previous_account_index: int | None,
    low_quota_percent: int = 5,
) -> CodexRotationObservation | None:
    rate_limits = (
        snapshot.rate_limited_responses - previous_rate_limits
        if previous_rate_limits is not None
        and snapshot.rate_limited_responses >= previous_rate_limits
        else 0
    )
    rotations = (
        snapshot.account_rotations - previous_rotations
        if previous_rotations is not None and snapshot.account_rotations >= previous_rotations
        else 0
    )
    switched = (
        previous_account_index is not None
        and snapshot.active_account_index is not None
        and previous_account_index != snapshot.active_account_index
    )
    source = next((item for item in pool.accounts if item.index == previous_account_index), None)
    quota_driven_switch = bool(
        switched
        and source is not None
        and (
            source.availability in {"unavailable", "rate-limited", "cooling-down", "delayed"}
            or (
                not source.quota_stale
                and (
                    source.five_hour_remaining is not None
                    and source.five_hour_remaining <= low_quota_percent
                    or source.weekly_remaining is not None
                    and source.weekly_remaining <= low_quota_percent
                )
            )
        )
    )
    if rate_limits <= 0 and rotations <= 0 and not quota_driven_switch:
        return None
    return CodexRotationObservation(
        source_account_index=previous_account_index if switched else None,
        target_account_index=snapshot.active_account_index,
        provider_limit_count=rate_limits,
        rotation_count=rotations,
    )


def read_codex_runtime_snapshot(root: Path) -> CodexRuntimeSnapshot | None:
    try:
        value = json.loads((root / "runtime-observability.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    metrics = value.get("runtimeMetrics")
    if not isinstance(metrics, dict):
        return None
    rate_limited = metrics.get("rateLimitedResponses")
    rotations = metrics.get("accountRotations")
    raw_index = value.get("lastAccountIndex")
    if not isinstance(rate_limited, int) or not isinstance(rotations, int):
        return None
    index = raw_index + 1 if isinstance(raw_index, int) else None
    return CodexRuntimeSnapshot(rate_limited, rotations, index)


def _identity(account: CodexAccountStatus | None, fallback: str) -> str:
    if account is None:
        return fallback
    return account.identity_hint or f"account {account.index}"


def format_codex_rotation_event(
    pool: CodexPoolStatus,
    observation: CodexRotationObservation,
) -> str:
    active = next(
        (item for item in pool.accounts if item.index == observation.target_account_index),
        next((item for item in pool.accounts if item.active), None),
    )
    exhausted = next(
        (
            item
            for item in pool.accounts
            if not item.active
            and (
                item.five_hour_remaining == 0
                or item.weekly_remaining == 0
                or item.availability in {"rate-limited", "cooling-down"}
            )
        ),
        None,
    )
    source_account = next(
        (item for item in pool.accounts if item.index == observation.source_account_index),
        exhausted,
    )
    source = _identity(source_account, "previous account")
    target = _identity(active, "another available account")
    limits: list[str] = []
    if active is not None and not active.quota_stale:
        if active.five_hour_remaining is not None:
            limits.append(f"5h {active.five_hour_remaining}%")
        if active.weekly_remaining is not None:
            limits.append(f"week {active.weekly_remaining}%")
    status = active.availability if active is not None else "unknown"
    status_text = f"{status}; {', '.join(limits)}" if limits else status
    if observation.provider_limit_count > 0:
        event = f"Codex quota exhausted for {source}; switched to {target}."
    else:
        event = f"Codex account switched from {source} to {target}."
    count = max(observation.provider_limit_count, observation.rotation_count)
    suffix = f" {count} provider events were coalesced." if count > 1 else ""
    return f"{event} Replacement status: {status_text}.{suffix}"


def codex_rotation_targets(
    state: HubState, operations: tuple[int, int] | None
) -> tuple[tuple[int, int], ...]:
    targets: list[tuple[int, int]] = []
    if operations is not None:
        targets.append(operations)
    active = state.active_topics_for_agent("codex")
    if len(active) == 1:
        work = (active[0].chat_id, active[0].thread_id)
        if work not in targets:
            targets.append(work)
    return tuple(targets)
