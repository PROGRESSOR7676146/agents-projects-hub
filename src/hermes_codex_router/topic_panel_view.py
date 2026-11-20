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

    headlines: dict[Headline, str] = {
        "needs_owner": "🔴 Нужно ваше решение",
        "stop_requested": "🟠 Остановка запрошена",
        "local_writer": "🟡 Управление в локальном терминале",
        "executing": "🟢 Выполняется",
        "preparing": "🟢 Подготовка запуска",
        "queued": "⏳ В очереди",
        "idle": "⚪ Нет активных задач",
    }
    headline = headlines[snapshot.headline]
    agent = _one_line(snapshot.agent)
    if agent and snapshot.headline not in ("idle", "local_writer"):
        headline = f"{headline} · {agent}"
    lines = [headline]

    if snapshot.headline == "needs_owner":
        reason_text: dict[OwnerReason, str] = {
            "uncertain": "Исход задачи неизвестен — решение в уведомлении выше",
            "delivery_unknown": "Доставка ответа не подтверждена — тема ждёт решения",
            "approval": "Агент ждёт разрешения",
        }
        seen: set[OwnerReason] = set()
        for reason in snapshot.owner_reasons:
            if reason not in seen:
                lines.append(reason_text[reason])
                seen.add(reason)

    model = _one_line(snapshot.model)
    if model and snapshot.headline not in ("idle", "local_writer"):
        lines.append(f"Модель: {model}")

    if snapshot.started_at is not None:
        lines.append(f"Запущено в {format_time(snapshot.started_at)}")

    if snapshot.headline in ("executing", "needs_owner", "stop_requested"):
        if snapshot.activity_at is not None:
            activity_labels: dict[str, str] = {
                "tool": "работа инструмента",
                "message": "сообщение агента",
                "approval": "ожидание разрешения",
            }
            label = activity_labels.get(snapshot.activity_kind or "")
            at = format_time(snapshot.activity_at)
            lines.append(
                f"Последнее событие: {label} · {at}" if label else f"Последнее событие · {at}"
            )

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
            if silent and latest is not None:
                lines.append(
                    f"Новых наблюдаемых событий нет с {format_time(latest)}. "
                    "Это не означает остановку."
                )
            elif (
                snapshot.activity_at is None
                and snapshot.interim_delivered_at is None
                and snapshot.started_at is not None
            ):
                lines.append("После запуска новых событий пока не было")

    if snapshot.headline == "executing" and snapshot.interim_delivered_at is not None:
        lines.append(
            f"Промежуточное сообщение доставлено · {format_time(snapshot.interim_delivered_at)}"
        )

    if snapshot.queued or snapshot.held:
        queue_line = f"Очередь: {snapshot.queued}"
        if snapshot.held:
            queue_line += f" · удержано: {snapshot.held}"
        lines.append(queue_line)

    if snapshot.headline == "idle":
        if snapshot.last_result == "delivered":
            result_line = "Последний ответ отправлен"
            if snapshot.last_result_at is not None:
                result_line += f" · {format_time(snapshot.last_result_at)}"
            lines.append(result_line)
        elif snapshot.last_result == "saved":
            lines.append("Последний ответ сохранён, доставка не подтверждена")

    lines = lines[:MAX_PANEL_LINES]
    while len("\n".join(lines)) > MAX_PANEL_CHARACTERS:
        lines.pop()
    return "\n".join(lines)


def panel_body_digest(body: str) -> str:
    """Return the SHA-256 hex digest of the UTF-8 rendered body."""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()
