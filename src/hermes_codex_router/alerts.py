from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from .codex_accounts import CodexPoolStatus

DEFAULT_LOW_QUOTA_PERCENT = 5
DEFAULT_CONTEXT_BLOAT_THRESHOLD = 65_000
DEFAULT_SESSION_SCAN_MAX_AGE_SECONDS = 7200
DEFAULT_MAX_TAIL_BYTES = 524288


@dataclass(frozen=True, slots=True)
class OperationalAlert:
    key: str
    code: str
    severity: str
    message: str


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _extract_latest_session_token_usage(
    path: Path, max_tail_bytes: int = DEFAULT_MAX_TAIL_BYTES
) -> tuple[str, int, int] | None:
    """Extract (session_id, input_tokens, total_tokens) from the tail of a rollout-*.jsonl file."""
    try:
        size = path.stat().st_size
    except OSError:
        return None
    try:
        with open(path, "rb") as f:
            f.seek(max(0, size - max_tail_bytes))
            chunk = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    for line in reversed(chunk.splitlines()):
        if '"token_usage_record"' in line:
            try:
                data = json.loads(line)
            except Exception:
                continue
            if data.get("type") == "token_usage_record":
                payload = data.get("payload")
                if isinstance(payload, dict):
                    usage = payload.get("usage")
                    if isinstance(usage, dict):
                        input_tokens = usage.get("input_tokens")
                        if isinstance(input_tokens, int):
                            session_id = str(
                                payload.get("session_id")
                                or payload.get("thread_id")
                                or path.stem.replace("rollout-", "")
                            )
                            total_tokens = usage.get("total_tokens")
                            total = total_tokens if isinstance(total_tokens, int) else input_tokens
                            return session_id, input_tokens, total
    return None


def _abbreviate_path(path_str: str) -> str:
    home = str(Path.home())
    if path_str == home:
        return "~"
    if path_str.startswith(home + "/"):
        return "~" + path_str[len(home) :]
    return path_str


def _clean_text(text: str, max_len: int = 60) -> str:
    line = text.splitlines()[0].strip()
    return line[:max_len] + ("..." if len(line) > max_len else "")


def _resolve_codex_session_label(
    session_id: str,
    sessions_dir: Path,
    *,
    state_snapshot: Mapping[str, object] | None = None,
    rollout_path: Path | None = None,
) -> str | None:
    """Return a descriptive human-readable label for a Codex session."""
    # 1. Check if session belongs to a Telegram topic managed by Hub
    if state_snapshot is not None:
        topics = state_snapshot.get("topics")
        if isinstance(topics, list):
            for t in topics:
                if not isinstance(t, dict):
                    continue
                provider_sid = t.get("provider_session_id")
                if provider_sid and str(provider_sid).strip() == session_id:
                    project = str(t.get("project_id") or "hub")
                    title = str(t.get("title") or "").strip()
                    thread_id = t.get("thread_id")
                    if title and thread_id is not None:
                        return f"Telegram [{project}: {title} #{thread_id}]"
                    if title:
                        return f"Telegram [{project}: {title}]"
                    return f"Telegram [{project} #{thread_id}]"

    # 2. Check Codex local session index (~/.codex/session_index.jsonl)
    thread_name: str | None = None
    codex_home = sessions_dir.parent
    idx_path = codex_home / "session_index.jsonl"
    if idx_path.is_file():
        try:
            with open(idx_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if session_id in line:
                        try:
                            data = json.loads(line)
                            if data.get("id") == session_id and data.get("thread_name"):
                                thread_name = _clean_text(str(data["thread_name"]))
                        except Exception:
                            continue
        except OSError:
            pass

    # 3. Check Codex local state db (~/.codex/state_5.sqlite) for name/title and cwd
    cwd: str | None = None
    db_path = codex_home / "state_5.sqlite"
    if db_path.is_file():
        try:
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as con:
                row = con.execute(
                    "SELECT name, title, cwd FROM threads WHERE id = ?", (session_id,)
                ).fetchone()
                if row:
                    if not thread_name and row[0]:
                        thread_name = _clean_text(str(row[0]))
                    elif not thread_name and row[1]:
                        thread_name = _clean_text(str(row[1]))
                    if row[2]:
                        cwd = str(row[2]).strip()
        except Exception:
            pass

    # 4. Fallback cwd from rollout file header if needed
    if not cwd and rollout_path and rollout_path.is_file():
        try:
            with open(rollout_path, "r", encoding="utf-8", errors="replace") as f:
                first_line = f.readline()
                if first_line:
                    meta = json.loads(first_line)
                    if meta.get("type") == "session_meta":
                        payload = meta.get("payload")
                        if isinstance(payload, dict) and payload.get("cwd"):
                            cwd = str(payload["cwd"]).strip()
        except Exception:
            pass

    parts: list[str] = []
    if thread_name:
        parts.append(f'"{thread_name}"')
    if cwd:
        parts.append(f"in {_abbreviate_path(cwd)}")

    joined = " ".join(parts)
    return f"CLI {joined}" if parts else None


def check_codex_session_bloat(
    sessions_dir: Path,
    *,
    threshold_tokens: int = DEFAULT_CONTEXT_BLOAT_THRESHOLD,
    max_age_seconds: int = DEFAULT_SESSION_SCAN_MAX_AGE_SECONDS,
    now: datetime | None = None,
    state_snapshot: Mapping[str, object] | None = None,
) -> tuple[OperationalAlert, ...]:
    if not sessions_dir.is_dir():
        return ()
    current_time = (now or datetime.now(timezone.utc)).timestamp()
    alerts: list[OperationalAlert] = []

    try:
        entries = list(sessions_dir.rglob("rollout-*.jsonl"))
    except OSError:
        return ()

    for entry in entries:
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if (current_time - mtime) > max_age_seconds:
            continue
        usage_info = _extract_latest_session_token_usage(entry)
        if usage_info is None:
            continue
        session_id, input_tokens, total_tokens = usage_info
        if input_tokens >= threshold_tokens:
            sid_short = session_id[:8]
            label = _resolve_codex_session_label(
                session_id,
                sessions_dir,
                state_snapshot=state_snapshot,
                rollout_path=entry,
            )
            name_part = f" ({label})" if label else ""
            alerts.append(
                OperationalAlert(
                    key=f"codex:session:{session_id}:bloat",
                    code="codex_context_bloat",
                    severity="warning",
                    message=(
                        f"Codex session {sid_short}{name_part} context size reached {input_tokens:,} tokens. "
                        "Run /compact or start a /new session to avoid rapid quota exhaustion and 429."
                    ),
                )
            )
    return tuple(alerts)


def evaluate_operational_alerts(
    *,
    pool: CodexPoolStatus,
    state_snapshot: Mapping[str, object],
    doctor_ok: bool,
    recovery_status: Mapping[str, bool] | None = None,
    telegram_access: Mapping[tuple[str, str], bool] | None = None,
    hermes_telegram: Mapping[str, object] | None = None,
    runtime_health: Mapping[str, object] | None = None,
    codex_config_proxy_ok: bool | None = None,
    now: datetime | None = None,
    low_quota_percent: int = DEFAULT_LOW_QUOTA_PERCENT,
    stuck_after_seconds: int = 15 * 60,
) -> tuple[OperationalAlert, ...]:
    evaluated_at = now or datetime.now(timezone.utc)
    alerts: list[OperationalAlert] = []
    if codex_config_proxy_ok is False:
        alerts.append(
            OperationalAlert(
                "codex:config-proxy",
                "codex_config_proxy_unavailable",
                "error",
                "Codex is configured for a local provider proxy that is unavailable; "
                "inspect the managed app-server before changing provider bindings.",
            )
        )
    if runtime_health is not None:
        deployment_revision = runtime_health.get("deployment_revision")
        if isinstance(deployment_revision, Mapping):
            revision_status = str(deployment_revision.get("status") or "unknown")
            if revision_status in {"mixed", "unknown"}:
                alerts.append(
                    OperationalAlert(
                        "deployment:revision",
                        f"deployment_revision_{revision_status}",
                        "error",
                        "Required Project Hub components report "
                        f"{revision_status} release identity; inspect local cached status "
                        "before acceptance.",
                    )
                )
        health_items: list[Mapping[str, object]] = []
        for name in ("controller", "monitor", "sender"):
            value = runtime_health.get(name)
            if isinstance(value, Mapping):
                health_items.append(value)
        workers = runtime_health.get("provider_workers")
        if isinstance(workers, list):
            health_items.extend(item for item in workers if isinstance(item, Mapping))
        for item in health_items:
            status = str(item.get("status") or "unknown")
            if status in {"healthy", "not_configured"}:
                continue
            component = str(item.get("component") or "runtime")[:32]
            instance_id = str(item.get("instance_id") or "unknown")[:128]
            agent_id = str(item.get("agent_id") or "")[:64]
            label = f"provider worker {agent_id}" if component == "provider_worker" else component
            alerts.append(
                OperationalAlert(
                    f"runtime:{component}:{instance_id}",
                    f"{component}_health_{status}",
                    "warning" if status == "degraded" else "error",
                    f"The configured {label} runtime health is {status}; "
                    "inspect the local cached status and service logs.",
                )
            )
    if not doctor_ok:
        alerts.append(
            OperationalAlert(
                "deployment:doctor",
                "deployment_unhealthy",
                "error",
                "Project Hub diagnostics are unhealthy; run the local doctor report.",
            )
        )
    if recovery_status is not None:
        hermes_ok = recovery_status.get("hermes", False)
        tlive_ok = recovery_status.get("tlive", False)
        if not hermes_ok:
            alerts.append(
                OperationalAlert(
                    "recovery:hermes",
                    "hermes_recovery_unavailable",
                    "warning",
                    "The independent Hermes Telegram recovery channel is unavailable.",
                )
            )
        if not tlive_ok:
            alerts.append(
                OperationalAlert(
                    "recovery:tlive",
                    "tlive_recovery_unavailable",
                    "warning",
                    "The tlive monitoring and remote-approval channel is unavailable.",
                )
            )
        if not hermes_ok and not tlive_ok:
            alerts.append(
                OperationalAlert(
                    "recovery:all",
                    "recovery_plane_unavailable",
                    "error",
                    "Both independent recovery channels are unavailable; local intervention is required.",
                )
            )
    if telegram_access is not None:
        for (agent_id, project_id), accessible in telegram_access.items():
            if accessible:
                continue
            alerts.append(
                OperationalAlert(
                    f"telegram:{agent_id}:{project_id}",
                    "telegram_bot_group_unavailable",
                    "error" if agent_id == "codex" else "warning",
                    f"The {agent_id} bot cannot access the {project_id} project group.",
                )
            )
    if hermes_telegram is not None:
        if hermes_telegram.get("policy_ok") is False:
            alerts.append(
                OperationalAlert(
                    "hermes:telegram:policy",
                    "hermes_group_policy_incomplete",
                    "error",
                    "Hermes does not allow every registered Telegram project group.",
                )
            )
        if hermes_telegram.get("heartbeat_ok") is False:
            alerts.append(
                OperationalAlert(
                    "hermes:telegram:heartbeat",
                    "hermes_gateway_heartbeat_stale",
                    "error",
                    "Hermes Gateway is active but its event-loop heartbeat is stale.",
                )
            )
        if hermes_telegram.get("api_ok") is False:
            alerts.append(
                OperationalAlert(
                    "hermes:telegram:api",
                    "hermes_telegram_api_unavailable",
                    "warning",
                    "Hermes Telegram Bot API liveness probe failed.",
                )
            )
        pending = hermes_telegram.get("pending_updates")
        if isinstance(pending, int) and pending > 0:
            alerts.append(
                OperationalAlert(
                    "hermes:telegram:pending",
                    "hermes_telegram_updates_pending",
                    "warning",
                    f"Hermes has {pending} Telegram update(s) waiting for its gateway.",
                )
            )
    if not pool.available and pool.error != "not_configured":
        alerts.append(
            OperationalAlert(
                "codex:pool",
                "codex_pool_unavailable",
                "error",
                "Codex account-pool status is unavailable.",
            )
        )
    elif pool.available:
        if not pool.rotation_enabled:
            alerts.append(
                OperationalAlert(
                    "codex:rotation",
                    "codex_rotation_disabled",
                    "error",
                    "Codex account rotation is disabled.",
                )
            )
        usable_replacement = any(account.availability == "ready" for account in pool.accounts)
        for account in pool.accounts:
            identity = f" ({account.identity_hint})" if account.identity_hint else ""
            if account.auth_invalidated:
                alerts.append(
                    OperationalAlert(
                        f"codex:account:{account.index}:token_invalid",
                        "codex_account_token_invalid",
                        "error",
                        f"Codex account {account.index}{identity} has an invalid or revoked token; "
                        f"re-authenticate via 'codex-multi-auth login --account {account.index} --device-auth'.",
                    )
                )
                continue
            fresh_quota_exhausted = not account.quota_stale and any(
                remaining is not None and remaining <= low_quota_percent
                for remaining in (account.five_hour_remaining, account.weekly_remaining)
            )
            # An inactive unavailable account is ordinary pool state while a
            # replacement is ready. Keep it visible in /accounts, but do not
            # page Operations or guess that authentication is broken.
            unavailable_alert_relevant = account.active or not usable_replacement
            if (
                account.availability == "unavailable"
                and unavailable_alert_relevant
                and not fresh_quota_exhausted
            ):
                alerts.append(
                    OperationalAlert(
                        f"codex:account:{account.index}:unavailable",
                        "codex_account_unavailable",
                        "error",
                        f"Codex account {account.index}{identity} is unavailable and no ready replacement exists; inspect quota and authentication state.",
                    )
                )
            quota_alert_relevant = account.active or not usable_replacement
            if (
                quota_alert_relevant
                and not account.quota_stale
                and account.five_hour_remaining is not None
                and account.five_hour_remaining <= low_quota_percent
            ):
                alerts.append(
                    OperationalAlert(
                        f"codex:account:{account.index}:5h-low",
                        "codex_5h_low",
                        "warning",
                        f"Codex account {account.index}{identity} has {account.five_hour_remaining}% of its 5-hour quota left.",
                    )
                )
            if (
                quota_alert_relevant
                and not account.quota_stale
                and account.weekly_remaining is not None
                and account.weekly_remaining <= low_quota_percent
            ):
                alerts.append(
                    OperationalAlert(
                        f"codex:account:{account.index}:week-low",
                        "codex_weekly_low",
                        "warning",
                        f"Codex account {account.index}{identity} has {account.weekly_remaining}% of its weekly quota left.",
                    )
                )
    pending = state_snapshot.get("pending_dispatches")
    if isinstance(pending, list):
        topic_lookup: dict[int, str] = {}
        topics = state_snapshot.get("topics")
        if isinstance(topics, list):
            for t in topics:
                if isinstance(t, dict) and t.get("topic_id") is not None:
                    try:
                        tid = int(t["topic_id"])
                        project = str(t.get("project_id") or "hub")
                        title = str(t.get("title") or "").strip()
                        topic_lookup[tid] = f"{project}: {title}" if title else project
                    except (ValueError, TypeError):
                        pass

        for item in pending:
            if not isinstance(item, dict):
                continue
            updated_at = _timestamp(item.get("updated_at"))
            if (
                updated_at is None
                or (evaluated_at - updated_at).total_seconds() <= stuck_after_seconds
            ):
                continue
            topic_id = item.get("topic_id")
            topic_desc = (
                f"topic {topic_id} ({topic_lookup[topic_id]})"
                if topic_id in topic_lookup
                else f"topic {topic_id}"
            )
            agent_id = str(item.get("agent_id") or "unknown")[:32]
            alerts.append(
                OperationalAlert(
                    f"dispatch:topic:{topic_id}:agent:{agent_id}",
                    "dispatch_stuck",
                    "error",
                    f"A {agent_id} dispatch in {topic_desc} has been running for over 15 minutes.",
                )
            )
    return tuple(alerts)
