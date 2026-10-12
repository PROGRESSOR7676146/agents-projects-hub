"""Bounded model-free incident notices; transport accepts no executable payload."""

from __future__ import annotations

import asyncio
import copy
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from functools import partial
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

from .hermes_incident_cursor import (
    CursorBusy,
    NoticeCapacity,
    NoticeCursor,
    NoticeSettings,
    fit_deliveries,
)
from .incident_journal import IncidentJournalError, _time, read_incident_snapshot
from .incident_runbooks import incident_runbook

SendNotice = Callable[[str, str], Awaitable[object]]
T = TypeVar("T")


async def file_call(call: Callable[..., T], *args: Any) -> T:
    """Keep disk I/O off Gateway's loop and join it even on repeated cancellation.

    These tasks own only bounded private-file I/O, never network/model authority.
    A stalled filesystem can delay shutdown, but cannot freeze the control loop
    or leave an unjoined write behind the next Gateway generation.
    """
    task = asyncio.get_running_loop().run_in_executor(None, partial(call, *args))
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except Exception:
            if cancelled:
                raise asyncio.CancelledError() from None
            raise
    if cancelled:
        raise asyncio.CancelledError()
    return result


@asynccontextmanager
async def _file_lock(store: NoticeCursor) -> AsyncIterator[None]:
    manager = store.locked()
    try:
        await file_call(manager.__enter__)
        yield
    finally:
        # __enter__ can finish acquiring while its await is cancelled. Joining
        # it, then always exiting the manager, releases that descriptor too.
        await file_call(manager.__exit__, None, None, None)


class NoticeRejected(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, min(86400, retry_after))
        super().__init__("incident_notice_rejected")


def _card(row: dict[str, Any]) -> str:
    guide = incident_runbook(row["code"])
    lines = [
        f"Инцидент · {row['severity']} · {row['code']}",
        f"Начало: {row['opened_at']}",
        "Наблюдённый сигнал:",
        row["message"],
        "",
        guide.title,
    ]
    for option in guide.options:
        prefix = "Рекомендуется" if option.option_id == guide.recommended_option else "Вариант"
        lines.append(f"{prefix}: {option.label}. {option.consequence}")
    if row["resolved_at"] is not None:
        lines.append(f"Уже наблюдалось закрытие: {row['resolved_at']}")
    return "\n".join(lines)


class HermesIncidentNotices:
    def __init__(self, settings: NoticeSettings) -> None:
        self.settings = settings
        self.store = NoticeCursor(settings)

    def _enqueue(
        self,
        value: dict[str, Any],
        text: str,
        members: list[str],
        now: datetime,
        kind: str = "episode",
    ) -> None:
        deliveries = value["deliveries"]
        if kind == "health":
            deliveries[:] = [
                row
                for row in deliveries
                if not (
                    row["kind"] == "health" and row["state"] == "pending" and not row["attempts"]
                )
            ]
        if len(text) > 3500:
            raise IncidentJournalError()
        deliveries.append(
            {
                "token": uuid.uuid4().hex,
                "text": text,
                "members": members,
                "state": "pending",
                "message_id": None,
                "attempts": 0,
                "retry_at": now.isoformat(),
                "kind": kind,
            }
        )
        fit_deliveries(value, reserve=4096)

    def _project(self, value: dict[str, Any], now: datetime) -> None:
        try:
            snapshot = read_incident_snapshot(self.settings.journal)
        except IncidentJournalError:
            if not value["quiet"]:
                self._enqueue(
                    value,
                    "Журнал инцидентов недоступен; состояние Hub неизвестно.",
                    [],
                    now,
                    "health",
                )
            value["quiet"] = True
            return
        updated = _time(snapshot["updated_at"])
        if updated > now + timedelta(seconds=5):
            raise IncidentJournalError()
        if value["journal_seed"] not in (None, snapshot["seed_binding"]):
            raise IncidentJournalError()
        value["journal_seed"] = snapshot["seed_binding"]
        quiet = now - updated >= timedelta(seconds=3 * self.settings.monitor_interval)
        if quiet != value["quiet"]:
            text = (
                "Журнал Hub не обновляется; текущая работоспособность неизвестна."
                if quiet
                else (
                    "Журнал Hub снова обновляется; это не подтверждение устранения всех инцидентов."
                )
            )
            self._enqueue(value, text, [], now, "health")
        value["quiet"] = quiet
        rows = snapshot["episodes"]
        old = value["seen"]
        changes: list[tuple[str, list[str]]] = []
        if not value["bootstrapped"]:
            open_ids = [r["id"] for r in rows if r["resolved_at"] is None]
            changes.append(
                (
                    f"Сводка наблюдённых инцидентов: открыто {len(open_ids)}. "
                    "Прошлые отдельные уведомления не повторяются.",
                    open_ids,
                )
            )
            value["bootstrapped"] = True
        else:
            for row in rows:
                prior = old.get(row["id"])
                if prior is None:
                    changes.append((_card(row), [row["id"]]))
                elif (
                    prior is not None
                    and not prior["resolved"]
                    and not prior["hidden"]
                    and row["resolved_at"] is not None
                ):
                    changes.append(
                        (
                            f"Наблюдённый инцидент закрыт · {row['code']}\n"
                            f"Время: {row['resolved_at']}",
                            [row["id"]],
                        )
                    )
        deferred = [
            r
            for r in value["deliveries"]
            if r["state"] == "pending" and not r["attempts"] and r["kind"] != "health"
        ]
        available = max(0, self.settings.max_cards - value["window_used"])
        if changes and (len(changes) + len(deferred) > available):
            # Coalesce only never-attempted notices. A rejected retry's exact text
            # and token, and all uncertain evidence, are immutable.
            members = sorted(
                set(m for r in deferred for m in r["members"])
                | set(m for _, ids in changes for m in ids)
            )[:500]
            for row in deferred:
                value["deliveries"].remove(row)
            text = (
                f"Сводка наблюдений: открыто {sum(r['resolved_at'] is None for r in rows)}. "
                f"За границей журнала: {snapshot['omitted_open_count']} открытых, "
                f"{snapshot['omitted_resolved_count']} закрытых. "
                "Отдельные карточки объединены; проверьте локальный журнал."
            )
            self._enqueue(value, text, members, now, "summary")
        else:
            for text, members in changes:
                self._enqueue(value, text, members, now)
        value["seen"] = {
            row["id"]: {
                "resolved": row["resolved_at"] is not None,
                "hidden": old.get(row["id"], {}).get("hidden", False),
            }
            for row in rows
        }

    async def tick(self, send: SendNotice, *, now: datetime | None = None) -> str:
        now = datetime.now(timezone.utc) if now is None else now
        try:
            if now.tzinfo is None:
                raise IncidentJournalError()
            now = now.astimezone(timezone.utc)
            async with _file_lock(self.store):
                value, saturated = await file_call(self._prepare, now)
                outcomes = {"unavailable" if saturated else "idle"}
                for row in tuple(value["deliveries"]):
                    if value["window_used"] >= self.settings.max_cards:
                        break
                    if row["state"] != "pending" or _time(row["retry_at"]) > now:
                        continue
                    row["state"] = "attempted"
                    row["attempts"] += 1
                    value["window_used"] += 1
                    await file_call(self.store.write, value)
                    try:
                        async with asyncio.timeout(3):
                            receipt = await send(row["text"], row["token"])
                    except NoticeRejected as error:
                        row["state"] = "pending" if row["attempts"] < 3 else "rejected"
                        row["retry_at"] = (now + timedelta(seconds=error.retry_after)).isoformat()
                        outcomes.add("rejected")
                    except asyncio.CancelledError:
                        row["state"] = "unknown"
                        await file_call(self.store.write, value)
                        raise
                    except Exception:
                        row["state"] = "unknown"
                        outcomes.add("unknown")
                    else:
                        if type(receipt) is int and 0 < receipt < 2**52:
                            row["state"], row["message_id"] = "sent", receipt
                            outcomes.add("sent")
                        else:
                            row["state"] = "unknown"
                            outcomes.add("unknown")
                    await file_call(self.store.write, value)
                return next(
                    s
                    for s in ("unavailable", "unknown", "rejected", "sent", "idle")
                    if s in outcomes
                )
        except CursorBusy:
            return "busy"
        except (
            OSError,
            ValueError,
            TypeError,
            RecursionError,
            OverflowError,
            IncidentJournalError,
        ):
            return "unavailable"

    def _prepare(self, now: datetime) -> tuple[dict[str, Any], bool]:
        value = self.store.read(now)
        if _time(value["updated_at"]) > now:
            raise IncidentJournalError()
        value["updated_at"] = now.isoformat()
        if now >= _time(value["window_start"]) + timedelta(seconds=self.settings.window_seconds):
            value["window_start"], value["window_used"] = now.isoformat(), 0
        for row in value["deliveries"]:
            if row["state"] == "attempted":
                row["state"] = "unknown"
        candidate = copy.deepcopy(value)
        try:
            self._project(candidate, now)
            fit_deliveries(candidate, reserve=4096)
            self.store.write(candidate)
        except NoticeCapacity:
            # Do not advance seen/quiet on a refused projection. Existing due
            # pending rows can still drain and free receipted capacity.
            return value, True
        return candidate, False

    def has_open_members(self, token: str) -> bool:
        # Called only inside tick's full-cycle lock, after the attempted fence.
        value = self.store.read(datetime.now(timezone.utc))
        return any(
            row["token"] == token
            and any(
                member in value["seen"] and not value["seen"][member]["resolved"]
                for member in row["members"]
            )
            for row in value["deliveries"]
        )

    def dismiss(self, token: str, actor: int, chat: int, message: int) -> bool:
        if (
            type(actor) is not int
            or actor != self.settings.owner
            or type(chat) is not int
            or chat != self.settings.owner
            or type(message) is not int
        ):
            return False
        try:
            with self.store.locked():
                value = self.store.read(datetime.now(timezone.utc))
                row = next(
                    (
                        r
                        for r in value["deliveries"]
                        if r["token"] == token
                        and r["state"] == "sent"
                        and r["message_id"] == message
                    ),
                    None,
                )
                if row is None or not any(
                    m in value["seen"] and not value["seen"][m]["resolved"] for m in row["members"]
                ):
                    return False
                for identifier in row["members"]:
                    if identifier in value["seen"]:
                        value["seen"][identifier]["hidden"] = True
                self.store.write(value)
                return True
        except (
            OSError,
            ValueError,
            TypeError,
            RecursionError,
            OverflowError,
            IncidentJournalError,
            CursorBusy,
        ):
            return False
