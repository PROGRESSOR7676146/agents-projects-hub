"""Passive exact-request delivery evidence; no provider or approval operations."""

from __future__ import annotations

import json
import math
import os
import re
import socket
import stat
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Mapping, Sequence

from .approval_observations import identity_digest
from .operational_alert import OperationalAlert

MAX_SNAPSHOT_BYTES = 131072
IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", re.ASCII)


@dataclass(frozen=True)
class ApprovalWait:
    thread_id: str
    turn_id: str
    request_digest: str
    item_digest: str
    observed_at: datetime


def read_snapshot(path: Path) -> dict | None:
    """One bounded read-only IPC exchange. Never opens tlive configuration/keys."""
    try:
        parent = path.parent.lstat()
        before = path.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077
            or not stat.S_ISSOCK(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o077
            or path.parent.resolve() != path.parent
        ):
            return None
        deadline = time.monotonic() + 2
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(path))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            client.settimeout(remaining)
            client.sendall(b'{"kind":"approval.snapshot"}\n')
            data = bytearray()
            while b"\n" not in data:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                client.settimeout(remaining)
                chunk = client.recv(min(4096, MAX_SNAPSHOT_BYTES + 1 - len(data)))
                if not chunk:
                    return None
                data.extend(chunk)
                if len(data) > MAX_SNAPSHOT_BYTES:
                    return None
        after = path.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            return None
        value = json.loads(data.split(b"\n", 1)[0])
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


def _number(value: object) -> bool:
    try:
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        )
    except OverflowError:
        return False


def assess_surface(
    waits: Sequence[ApprovalWait],
    snapshot: Mapping | None,
    *,
    now: datetime,
    grace_seconds: int = 30,
) -> dict[str, int]:
    """Fresh grace is not health. A new daemon cannot inherit old positive proof."""
    result = dict(waiting=len(waits), grace=0, sent=0, unavailable=0, unknown=0)
    current = now.timestamp() * 1000
    valid = bool(
        snapshot
        and snapshot.get("kind") == "approval.snapshot"
        and type(snapshot.get("version")) is int
        and snapshot.get("version") == 1
        and isinstance(snapshot.get("epoch"), str)
        and re.fullmatch(r"[0-9a-f-]{36}", snapshot["epoch"])
        and _number(snapshot.get("capturedAt"))
        and _number(snapshot.get("epochStartedAt"))
        and 0 <= current - snapshot["capturedAt"] <= 5000
        and 0 <= snapshot["epochStartedAt"] <= snapshot["capturedAt"]
        and isinstance(snapshot.get("entries"), list)
        and len(snapshot["entries"]) <= 128
        and snapshot.get("truncated") is False
    )
    entries = snapshot["entries"] if valid and snapshot else []
    for wait in waits:
        age = (now - wait.observed_at).total_seconds()
        if 0 <= age < grace_seconds:
            result["grace"] += 1
            continue
        matches = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            request = entry.get("providerRequestId")
            item = entry.get("itemId")
            if not (
                (type(request) is int and -(2**53) < request < 2**53)
                or (isinstance(request, str) and IDENTITY.fullmatch(request))
            ) or not (isinstance(item, str) and IDENTITY.fullmatch(item)):
                continue
            if (
                entry.get("threadId") == wait.thread_id
                and entry.get("turnId") == wait.turn_id
                and identity_digest(request) == wait.request_digest
                and identity_digest(item) == wait.item_digest
            ):
                matches.append(entry)
        if len(matches) != 1:
            result["unknown"] += 1
            continue
        entry = matches[0]
        assert snapshot is not None
        times = (
            snapshot["epochStartedAt"],
            entry.get("observedAt"),
            entry.get("deliveryAt"),
            snapshot["capturedAt"],
        )
        if not all(_number(value) for value in times) or not all(
            a <= b for a, b in zip(times, times[1:])
        ):
            result["unknown"] += 1
        elif entry.get("delivery") == "sent":
            result["sent"] += 1
        elif (
            entry.get("delivery") == "pending"
            and _number(entry.get("graceUntil"))
            and entry["observedAt"]
            <= current
            < entry["graceUntil"]
            <= entry["observedAt"] + 86400000
        ):
            result["grace"] += 1
        elif entry.get("delivery") in ("pending", "sending", "failed"):
            result["unavailable"] += 1
        else:
            result["unknown"] += 1
    return result


def surface_alerts(counts: Mapping[str, int]) -> tuple[OperationalAlert, ...]:
    if not (counts["unavailable"] or counts["unknown"]):
        return ()
    return (
        OperationalAlert(
            "tlive:approval-surface",
            "tlive_approval_surface_unconfirmed",
            "warning",
            "A waiting Codex permission has no confirmed remote card delivery. "
            "Inspect the exact native request and tlive delivery diagnostics; keep unknown work blocked. "
            "Do not replay the task or approve automatically.",
        ),
    )
