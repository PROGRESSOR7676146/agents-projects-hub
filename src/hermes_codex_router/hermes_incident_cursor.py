"""Hermes-owned private notice fences; never Hub task or approval authority."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .incident_journal import IncidentJournalError, _private_parent, _private_stat, _read, _time

MAX_CURSOR_BYTES = 1024 * 1024
MAX_DELIVERIES = 128
ID = re.compile(r"[0-9a-f]{64}\.[0-9a-f]{32}\Z")
TOKEN = re.compile(r"[0-9a-f]{32}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class NoticeSettings:
    journal: Path
    cursor: Path
    owner: int
    interval: int = 60
    monitor_interval: int = 60
    max_cards: int = 3
    window_seconds: int = 300

    def __post_init__(self) -> None:
        if type(self.owner) is not int or not 0 < self.owner < 2**52:
            raise ValueError("incident_notice_configuration_invalid")
        for path in (self.journal, self.cursor):
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("incident_notice_configuration_invalid")
        journal_paths = {
            self.journal,
            self.journal.with_suffix(".seed"),
            self.journal.with_suffix(".lock"),
        }
        if {
            self.cursor,
            self.cursor.with_suffix(".lock"),
        } & journal_paths or self.cursor.with_suffix(".lock") == self.cursor:
            raise ValueError("incident_notice_configuration_invalid")
        for value, minimum, maximum in (
            (self.interval, 5, 3600),
            (self.monitor_interval, 5, 3600),
            (self.max_cards, 1, 10),
            (self.window_seconds, 60, 3600),
        ):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError("incident_notice_configuration_invalid")

    @property
    def binding(self) -> str:
        # Policy changes require a new reviewed cursor as well as a restart.
        return hashlib.sha256(repr(self).encode()).hexdigest()


class CursorBusy(RuntimeError):
    pass


class NoticeCapacity(IncidentJournalError):
    """Projection cannot fit without erasing unreceipted delivery evidence."""


def _encoded(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def fit_deliveries(value: dict[str, Any], *, reserve: int = 0) -> None:
    # Projection reserves room for later fence/outcome growth; writes consume it.
    # Only proven sent rows may be evicted, oldest first, at either bound.
    rows = value["deliveries"]
    while len(rows) > MAX_DELIVERIES or len(_encoded(value)) > MAX_CURSOR_BYTES - reserve:
        removable = next((row for row in rows if row["state"] == "sent"), None)
        if removable is None:
            raise NoticeCapacity()
        rows.remove(removable)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise IncidentJournalError()
        result[key] = value
    return result


def _new(settings: NoticeSettings, now: datetime) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "binding": settings.binding,
        "updated_at": now.isoformat(),
        "journal_seed": None,
        "bootstrapped": False,
        "quiet": False,
        "window_start": now.isoformat(),
        "window_used": 0,
        "seen": {},
        "deliveries": [],
    }


def _validate(value: Any, settings: NoticeSettings) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(
        _new(settings, datetime.now().astimezone())
    ):
        raise IncidentJournalError()
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise IncidentJournalError()
    if value["binding"] != settings.binding:
        raise IncidentJournalError()
    if value["journal_seed"] is not None and (
        not isinstance(value["journal_seed"], str) or not DIGEST.fullmatch(value["journal_seed"])
    ):
        raise IncidentJournalError()
    for name in ("bootstrapped", "quiet"):
        if type(value[name]) is not bool:
            raise IncidentJournalError()
    if not _time(value["window_start"]) <= _time(value["updated_at"]):
        raise IncidentJournalError()
    used = value["window_used"]
    if type(used) is not int or not 0 <= used <= settings.max_cards:
        raise IncidentJournalError()
    seen = value["seen"]
    if not isinstance(seen, dict) or len(seen) > 500:
        raise IncidentJournalError()
    for key, item in seen.items():
        if (
            not ID.fullmatch(key)
            or not isinstance(item, dict)
            or set(item) != {"resolved", "hidden"}
        ):
            raise IncidentJournalError()
        if any(type(v) is not bool for v in item.values()):
            raise IncidentJournalError()
    deliveries = value["deliveries"]
    if not isinstance(deliveries, list) or len(deliveries) > MAX_DELIVERIES:
        raise IncidentJournalError()
    tokens: set[str] = set()
    for row in deliveries:
        if not isinstance(row, dict) or set(row) != {
            "token",
            "text",
            "members",
            "state",
            "message_id",
            "attempts",
            "retry_at",
            "kind",
        }:
            raise IncidentJournalError()
        token, text = row["token"], row["text"]
        if not isinstance(token, str) or not TOKEN.fullmatch(token) or token in tokens:
            raise IncidentJournalError()
        tokens.add(token)
        if not isinstance(text, str) or not 0 < len(text) <= 3500:
            raise IncidentJournalError()
        members = row["members"]
        if not isinstance(members, list) or len(members) > 500:
            raise IncidentJournalError()
        if any(not isinstance(m, str) or not ID.fullmatch(m) for m in members):
            raise IncidentJournalError()
        if len(set(members)) != len(members):
            raise IncidentJournalError()
        if row["state"] not in {"pending", "attempted", "sent", "unknown", "rejected"}:
            raise IncidentJournalError()
        if row["kind"] not in {"episode", "summary", "health"}:
            raise IncidentJournalError()
        message = row["message_id"]
        if row["state"] == "sent":
            if type(message) is not int or not 0 < message < 2**52:
                raise IncidentJournalError()
        elif message is not None:
            raise IncidentJournalError()
        if type(row["attempts"]) is not int or not 0 <= row["attempts"] <= 3:
            raise IncidentJournalError()
        if row["state"] != "pending" and not row["attempts"]:
            raise IncidentJournalError()
        if row["state"] == "pending" and row["attempts"] == 3:
            raise IncidentJournalError()
        if row["state"] == "rejected" and row["attempts"] != 3:
            raise IncidentJournalError()
        _time(row["retry_at"])
    return value


class NoticeCursor:
    def __init__(self, settings: NoticeSettings) -> None:
        self.settings = settings
        self.path = settings.cursor

    @contextmanager
    def locked(self) -> Iterator[None]:
        _private_parent(self.path)
        fd = os.open(
            self.path.with_suffix(".lock"),
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o600,
        )
        try:
            _private_stat(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CursorBusy() from None
            yield
        finally:
            os.close(fd)

    def read(self, now: datetime) -> dict[str, Any]:
        try:
            raw = _read(self.path, MAX_CURSOR_BYTES)
        except FileNotFoundError:
            return _new(self.settings, now)
        # Preserve unreadable delivery evidence; only an absent cursor cold-seeds.
        value = json.loads(raw, object_pairs_hook=_pairs)
        return _validate(value, self.settings)

    def write(self, value: dict[str, Any]) -> None:
        fit_deliveries(value)
        _validate(value, self.settings)
        raw = _encoded(value)
        fd, name = tempfile.mkstemp(prefix=".hermes-incident-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)
