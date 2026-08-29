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


def format_codex_rotation_event(pool: CodexPoolStatus, count: int) -> str:
    active = next((item for item in pool.accounts if item.active), None)
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
    source = _identity(exhausted, "previous account")
    target = _identity(active, "another available account")
    suffix = f" ({count} provider limit event(s))" if count > 1 else ""
    return f"Codex quota exhausted for {source}; rotated to {target}{suffix}."


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
