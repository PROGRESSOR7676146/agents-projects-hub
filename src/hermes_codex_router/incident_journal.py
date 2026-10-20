"""Private derived alert episodes; no execution, delivery or approval authority."""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .operational_alert import OperationalAlert

MAX_BYTES = 1024 * 1024
MAX_EPISODES = 500
RETENTION = timedelta(days=30)
_ID = re.compile(r"[0-9a-f]{64}\.[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SEED_DOMAIN = b"agents-projects-hub/incident-journal/seed-binding/v1"
_CODE = re.compile(r"[a-z][a-z0-9_]{0,95}\Z")
_ROW_FIELDS = {"id", "code", "severity", "message", "opened_at", "last_seen_at", "resolved_at"}
_FIELDS = {
    "schema_version",
    "seed_binding",
    "updated_at",
    "omitted_open_count",
    "omitted_resolved_count",
    "episodes",
}


class IncidentJournalError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("incident_journal_unavailable")


class IncidentJournalBusy(RuntimeError):
    def __init__(self) -> None:
        super().__init__("monitor_busy")


def _private_stat(fd: int) -> os.stat_result:
    value = os.fstat(fd)
    if (
        not stat.S_ISREG(value.st_mode)
        or value.st_uid != os.geteuid()
        or value.st_mode & 0o077
        or value.st_nlink != 1
    ):
        raise IncidentJournalError()
    return value


def _read(path: Path, limit: int) -> bytes:
    # NONBLOCK plus fstat refuses FIFOs/devices before a read can block.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        if _private_stat(fd).st_size > limit:
            raise IncidentJournalError()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            value = stream.read(limit + 1)
        if len(value) > limit:
            raise IncidentJournalError()
        return value
    finally:
        os.close(fd)


def _private_parent(path: Path) -> None:
    parent = path.parent
    if not path.is_absolute() or parent.resolve() != parent:
        raise IncidentJournalError()
    value = parent.stat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != os.geteuid() or value.st_mode & 0o077:
        raise IncidentJournalError()


def _time(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise IncidentJournalError()
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise IncidentJournalError()
    return parsed.astimezone(timezone.utc)


def _valid_message(code: object, severity: object, message: object) -> bool:
    return (
        isinstance(code, str)
        and _CODE.fullmatch(code) is not None
        and severity in {"warning", "error", "info"}
        and isinstance(message, str)
        and 0 < len(message.encode("utf-8")) <= 1024
        and all(ord(char) >= 32 for char in message)
    )


def _load(raw: bytes) -> dict[str, Any]:
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or set(value) != _FIELDS
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["seed_binding"], str)
        or _DIGEST.fullmatch(value["seed_binding"]) is None
    ):
        raise IncidentJournalError()
    updated = _time(value["updated_at"])
    for name in ("omitted_open_count", "omitted_resolved_count"):
        if type(value[name]) is not int or value[name] < 0:
            raise IncidentJournalError()
    rows = value["episodes"]
    if not isinstance(rows, list) or len(rows) > MAX_EPISODES:
        raise IncidentJournalError()
    identifiers: set[str] = set()
    open_keys: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != _ROW_FIELDS:
            raise IncidentJournalError()
        identifier = row["id"]
        if (
            not isinstance(identifier, str)
            or _ID.fullmatch(identifier) is None
            or identifier in identifiers
            or not _valid_message(row["code"], row["severity"], row["message"])
        ):
            raise IncidentJournalError()
        identifiers.add(identifier)
        opened, seen = _time(row["opened_at"]), _time(row["last_seen_at"])
        if not opened <= seen <= updated:
            raise IncidentJournalError()
        if row["resolved_at"] is None:
            prefix = identifier.split(".")[0]
            if prefix in open_keys:
                raise IncidentJournalError()
            open_keys.add(prefix)
        elif not seen <= _time(row["resolved_at"]) <= updated:
            raise IncidentJournalError()
    return value


def read_incident_snapshot(path: Path) -> dict[str, Any]:
    """Read one validated private projection without touching seed, lock or state."""
    try:
        _private_parent(path)
        return _load(_read(path, MAX_BYTES))
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        raise IncidentJournalError() from None


class IncidentJournal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._locked = False

    @contextmanager
    def cycle(self) -> Iterator[IncidentJournal]:
        """Serialize complete monitor cycles, not only snapshot replacement."""
        fd: int | None = None
        try:
            fd = os.open(
                self.path.with_suffix(".lock"),
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                0o600,
            )
            _private_stat(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise IncidentJournalBusy() from None
        except OSError:
            if fd is not None:
                os.close(fd)
            raise IncidentJournalError() from None
        except (IncidentJournalBusy, IncidentJournalError):
            if fd is not None:
                os.close(fd)
            raise
        try:
            self._locked = True
            yield self
        finally:
            self._locked = False
            if fd is not None:
                os.close(fd)

    def _seed(self, *, history_exists: bool) -> bytes:
        seed_path = self.path.with_suffix(".seed")
        try:
            value = _read(seed_path, 32)
        except FileNotFoundError:
            if history_exists:
                raise IncidentJournalError() from None
            value = os.urandom(32)
            fd = os.open(seed_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
        if len(value) != 32:
            raise IncidentJournalError()
        return value

    def _publish(self, value: dict[str, Any]) -> None:
        data = (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        if len(data) > MAX_BYTES:
            raise IncidentJournalError()
        fd, name = tempfile.mkstemp(prefix=".incident-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)

    def project(
        self,
        alerts: tuple[OperationalAlert, ...],
        *,
        now: datetime,
        unobserved_keys: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        try:
            return self._project(alerts, now=now, unobserved_keys=unobserved_keys)
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            raise IncidentJournalError() from None

    def _project(
        self,
        alerts: tuple[OperationalAlert, ...],
        *,
        now: datetime,
        unobserved_keys: tuple[str, ...],
    ) -> dict[str, Any]:
        if not self._locked or now.tzinfo is None:
            raise IncidentJournalError()
        now = now.astimezone(timezone.utc)
        try:
            previous = _load(_read(self.path, MAX_BYTES))
        except FileNotFoundError:
            previous = None
        if previous is not None and _time(previous["updated_at"]) > now:
            raise IncidentJournalError()
        seed = self._seed(history_exists=previous is not None)
        seed_binding = hmac.new(seed, _SEED_DOMAIN, hashlib.sha256).hexdigest()
        if previous is not None and not hmac.compare_digest(previous["seed_binding"], seed_binding):
            raise IncidentJournalError()
        current: dict[str, OperationalAlert] = {}
        unobserved = {
            hmac.new(seed, key.encode("utf-8"), hashlib.sha256).hexdigest()
            for key in unobserved_keys
        }
        for alert in alerts:
            if not _valid_message(alert.code, alert.severity, alert.message):
                raise IncidentJournalError()
            token = hmac.new(seed, alert.key.encode("utf-8"), hashlib.sha256).hexdigest()
            if token in current and current[token] != alert:
                raise IncidentJournalError()
            current[token] = alert
        stamp = now.isoformat()
        old_rows = [] if previous is None else previous["episodes"]
        active: dict[str, dict[str, Any]] = {}
        resolved: list[dict[str, Any]] = []
        for row in old_rows:
            token = row["id"].split(".")[0]
            if row["resolved_at"] is None and (token in current or token in unobserved):
                active[token] = row
                continue
            if row["resolved_at"] is None:
                row["resolved_at"] = stamp
            if _time(row["resolved_at"]) >= now - RETENTION:
                resolved.append(row)
        # Select opaque keys before creating rows: unchanged storms retain IDs.
        selected = sorted(current.keys() | active.keys())[:MAX_EPISODES]
        open_rows: list[dict[str, Any]] = []
        for token in selected:
            if token not in current:
                # Unknown is neither a fresh failure observation nor recovery.
                # Preserve the episode, including the old last_seen_at.
                open_rows.append(active[token])
                continue
            alert = current[token]
            row = active.get(token) or {"id": token + "." + uuid.uuid4().hex, "opened_at": stamp}
            row.update(
                code=alert.code,
                severity=alert.severity,
                message=alert.message,
                last_seen_at=stamp,
                resolved_at=None,
            )
            open_rows.append(row)
        resolved.sort(key=lambda row: (row["resolved_at"], row["id"]), reverse=True)
        keep_resolved = resolved[: MAX_EPISODES - len(open_rows)]
        result = {
            "schema_version": 1,
            "seed_binding": seed_binding,
            "updated_at": stamp,
            "omitted_open_count": len(current.keys() | active.keys()) - len(open_rows),
            "omitted_resolved_count": len(resolved) - len(keep_resolved),
            "episodes": open_rows + keep_resolved,
        }
        self._publish(result)
        return result
