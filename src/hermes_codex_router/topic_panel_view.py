"""Read-only topic status panel: snapshot DTO and pure Russian render (ADR 0110).

The snapshot holds only durable Hub evidence. Rendering is pure: no state,
transport, provider or model access. Times are absolute so the displayed body
changes only when the observed state changes, never with the wall clock alone.
"""

from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

Headline = Literal[
    "needs_owner",
    "stop_requested",
    "local_writer",
    "executing",
    "preparing",
    "queued",
    "idle",
]
OwnerReason = Literal["uncertain", "delivery_unknown", "approval"]
ActivityKind = Literal["tool", "message", "approval"]
LastResult = Literal["delivered", "saved"]

PANEL_TIMEZONE = "Europe/Moscow"  # the same display zone as /status
SILENCE_HIGHLIGHT_SECONDS = 600  # a display threshold, not an SLA (ADR 0110)
MAX_PANEL_LINES = 10
MAX_PANEL_CHARACTERS = 1500


@dataclass(frozen=True, slots=True)
class PanelSnapshot:
    """Observed facts for one topic at ``observed_at``; ``None`` means unknown."""

    observed_at: datetime
    headline: Headline
    owner_reasons: tuple[OwnerReason, ...] = ()
    agent: str | None = None
    model: str | None = None
    started_at: datetime | None = None
    activity_kind: ActivityKind | None = None
    activity_at: datetime | None = None
    interim_delivered_at: datetime | None = None
    queued: int = 0
    held: int = 0
    last_result: LastResult | None = None
    last_result_at: datetime | None = None


MAX_IDENTITY_CHARACTERS = 80


def _one_line(value: str | None) -> str:
    """Collapse untrusted identity text to one bounded, escaped line."""
    if not value:
        return ""
    collapsed = " ".join(value.split())[:MAX_IDENTITY_CHARACTERS]
    return html.escape(collapsed, quote=False)


def render_panel_body(snapshot: PanelSnapshot, *, timezone: ZoneInfo | None = None) -> str:
    """Render durable topic evidence as a bounded Telegram HTML body."""
    timestamps = (
        snapshot.observed_at,
        snapshot.started_at,
        snapshot.activity_at,
        snapshot.interim_delivered_at,
        snapshot.last_result_at,
    )
    for timestamp in timestamps:
        if timestamp is not None and (timestamp.tzinfo is None or timestamp.utcoffset() is None):
            raise ValueError("Panel timestamps must be timezone-aware")

    display_timezone = timezone if timezone is not None else ZoneInfo(PANEL_TIMEZONE)
    observed_day = snapshot.observed_at.astimezone(display_timezone).date()

    def format_time(timestamp: datetime) -> str:
        local = timestamp.astimezone(display_timezone)
        format_string = "%H:%M" if local.date() == observed_day else "%d.%m %H:%M"
        return local.strftime(format_string)

    # One fact per segment, each time shown once (owner feedback 2026-11-23).
    headlines: dict[Headline, str] = {
        "needs_owner": "🔴 Нужно решение",
        "stop_requested": "🟠 Остановка запрошена",
        "local_writer": "🟡 Локальный терминал",
        "executing": "🟢",
        "preparing": "🟢 Запуск",
        "queued": "⏳ В очереди",
        "idle": "⚪ Свободно",
    }
    with_identity = snapshot.headline not in ("idle", "local_writer")
    agent = _one_line(snapshot.agent) if with_identity else ""
    model = _one_line(snapshot.model) if with_identity else ""
    if agent and model.casefold().startswith(agent.casefold() + "-"):
        model = model[len(agent) + 1 :]  # "Claude claude-x" repeats the agent
    identity = " ".join(part for part in (agent, model) if part)
    status = headlines[snapshot.headline]
    counts = ""
    if snapshot.queued or snapshot.held:
        counts = f"{snapshot.queued}" + (f", удержано {snapshot.held}" if snapshot.held else "")
    if snapshot.headline == "queued" and counts:
        status = f"{status}: {counts}"  # the headline already names the queue
    if identity:
        status = (
            f"{status} {identity}"
            if snapshot.headline == "executing"
            else (f"{status} · {identity}")
        )
    first = [status]
    start = format_time(snapshot.started_at) if snapshot.started_at is not None else None
    if start is not None:
        first.append(f"с {start}")
    if snapshot.headline == "idle":
        if snapshot.last_result == "delivered":
            first.append(
                "ответ отправлен"
                + (f" {format_time(snapshot.last_result_at)}" if snapshot.last_result_at else "")
            )
        elif snapshot.last_result == "saved":
            first.append("ответ сохранён, доставка не подтверждена")
    lines = [" · ".join(first)]

    # An approval wait that is also the reason to ask the owner is said once, with its time.
    approval_at = (
        snapshot.activity_at
        if snapshot.headline == "needs_owner"
        and snapshot.activity_kind == "approval"
        and "approval" in snapshot.owner_reasons
        else None
    )
    if snapshot.headline == "needs_owner":
        reason_text: dict[OwnerReason, str] = {
            "uncertain": "исход неизвестен, решение в уведомлении выше",
            "delivery_unknown": "доставка не подтверждена, тема ждёт решения",
            "approval": "агент ждёт разрешения"
            + (f" с {format_time(approval_at)}" if approval_at is not None else ""),
        }
        reasons = [reason_text[reason] for reason in dict.fromkeys(snapshot.owner_reasons)]
        if reasons:
            reason_line = "; ".join(reasons)
            lines.append(reason_line[:1].upper() + reason_line[1:])

    silent = False
    if snapshot.headline == "executing":
        evidence = [
            timestamp
            for timestamp in (
                snapshot.activity_at,
                snapshot.interim_delivered_at,
                snapshot.started_at,
            )
            if timestamp is not None
        ]
        latest = (
            max(evidence, key=lambda timestamp: timestamp.astimezone(UTC)) if evidence else None
        )
        silent = (
            latest is not None
            and (snapshot.observed_at.astimezone(UTC) - latest.astimezone(UTC)).total_seconds()
            >= SILENCE_HIGHLIGHT_SECONDS
        )

    details: list[str] = []
    if snapshot.headline in ("executing", "needs_owner", "stop_requested"):
        # Activity kinds stay distinct facts (ADR 0110); only equal times are shared.
        events: list[tuple[str, str]] = []
        if snapshot.activity_at is not None and approval_at is None:
            activity_labels: dict[str, str] = {
                "tool": "инструмент",
                "message": "сообщение",
                "approval": "запрос разрешения",
            }
            label = activity_labels.get(snapshot.activity_kind or "", "событие")
            events.append((label, format_time(snapshot.activity_at)))
        if snapshot.headline == "executing" and snapshot.interim_delivered_at is not None:
            events.append(("доставлено", format_time(snapshot.interim_delivered_at)))
        groups: list[tuple[list[str], str]] = []
        for label, at in events:
            if groups and groups[-1][1] == at:
                groups[-1][0].append(label)
            else:
                groups.append(([label], at))
        for labels, at in groups:
            details.append(", ".join(labels) + ("" if at == start else f" {at}"))
        if (
            not events
            and not silent
            and snapshot.headline == "executing"
            and snapshot.started_at is not None
        ):
            details.append("событий пока нет")

    if counts and snapshot.headline != "queued":
        details.append(f"в очереди {counts}")
    if details:
        detail_line = " · ".join(details)
        lines.append(detail_line[:1].upper() + detail_line[1:])

    if silent:
        # A fixed phrase: the body changes once at the threshold, not every minute.
        lines.append(f"Новых событий нет {SILENCE_HIGHLIGHT_SECONDS // 60}+ мин. Это не остановка.")

    lines = lines[:MAX_PANEL_LINES]
    while len("\n".join(lines)) > MAX_PANEL_CHARACTERS:
        lines.pop()
    return "\n".join(lines)


def panel_body_digest(body: str) -> str:
    """Return the SHA-256 hex digest of the UTF-8 rendered body."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


PANEL_REFRESH_PREFIX = "pn:r:"


def render_panel_message(
    body: str, *, prepared_at: datetime, timezone: ZoneInfo | None = None
) -> str:
    """Append when this changed version was prepared; it is outside the body digest."""
    if prepared_at.tzinfo is None or prepared_at.utcoffset() is None:
        raise ValueError("Panel timestamps must be timezone-aware")
    local = prepared_at.astimezone(timezone if timezone is not None else ZoneInfo(PANEL_TIMEZONE))
    return f"{body}\n<i>изменено {local:%d.%m %H:%M}</i>"


def panel_keyboard(instance: int) -> dict[str, object]:
    """The single owner-only read-only control, bound to one panel instance."""
    if instance < 1:
        raise ValueError("panel instance must be positive")
    return {
        "inline_keyboard": [
            [{"text": "Обновить", "callback_data": f"{PANEL_REFRESH_PREFIX}{instance}"}]
        ]
    }
