"""Producer-owned causal evidence, within the existing poll transaction."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .state_errors import StateError


@dataclass(frozen=True, slots=True, order=True)
class PollCursor:
    epoch: int
    sequence: int


def evidence_time(value: str | None, *, now: datetime, future: bool = False) -> datetime | None:
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise StateError("invalid retained ingress clock") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise StateError("retained ingress clock must be timezone-aware")
    result = result.astimezone(timezone.utc)
    if not future and result > now + timedelta(seconds=5):
        raise StateError("retained ingress clock is in the future")
    return result


def row_cursor(
    row: sqlite3.Row, epoch: str, sequence: str, *, minimum: int = 1
) -> PollCursor | None:
    values = row[epoch], row[sequence]
    if values == (None, None):
        return None
    if (
        any(type(value) is not int for value in values)
        or not 1 <= values[0] < 2**63
        or not minimum <= values[1] < 2**63
    ):
        raise StateError("invalid retained ingress cursor")
    return PollCursor(values[0], values[1])


@dataclass(frozen=True, slots=True)
class IngressWatermark:
    success: PollCursor | None = None
    success_at: datetime | None = None
    failure: PollCursor | None = None
    failure_threshold_at: datetime | None = None


class TelegramIngressWatermarks:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.db = connection

    def read(self, identity: str, *, ledger: sqlite3.Row | None, now: datetime) -> IngressWatermark:
        row = self.db.execute(
            "SELECT * FROM telegram_ingress_watermarks WHERE identity=?", (identity,)
        ).fetchone()
        if row is None:
            return IngressWatermark()
        if ledger is None:
            raise StateError("ingress watermark has no retained ledger")
        current = row_cursor(ledger, "epoch", "poll_sequence", minimum=0)
        assert current is not None
        success = row_cursor(row, "success_epoch", "success_sequence")
        failure = row_cursor(row, "failure_witness_epoch", "failure_witness_sequence")
        success_at = evidence_time(row["success_at"], now=now)
        threshold = evidence_time(row["failure_threshold_at"], now=now)
        confirmed = evidence_time(ledger["last_confirmed_poll_at"], now=now)
        latest = evidence_time(ledger["last_poll_at"] or ledger["registered_at"], now=now)
        if (
            (success is None) != (success_at is None)
            or (failure is None) != (threshold is None)
            or (success is not None and success > current)
            or (failure is not None and failure > current)
            or (success_at is not None and (confirmed is None or success_at > confirmed))
            or (threshold is not None and (latest is None or threshold > latest))
            or (failure is not None and success is not None and failure <= success)
            or (threshold is not None and success_at is not None and threshold < success_at)
        ):
            raise StateError("incoherent retained ingress watermark")
        return IngressWatermark(success, success_at, failure, threshold)

    def record_in_transaction(
        self,
        identity: str,
        *,
        cursor: PollCursor,
        succeeded: bool,
        observed_at: datetime,
        threshold_at: str | None,
    ) -> None:
        if not self.db.in_transaction:
            raise StateError("ingress watermark requires the poll owner transaction")
        ledger = self.db.execute(
            "SELECT * FROM telegram_group_ingress WHERE identity=?", (identity,)
        ).fetchone()
        prior = self.read(identity, ledger=ledger, now=observed_at)
        success, success_at = prior.success, prior.success_at
        failure, threshold = prior.failure, prior.failure_threshold_at
        if succeeded:
            success, success_at = cursor, observed_at
            failure, threshold = None, None
        elif failure is None and threshold_at is not None:
            failure, threshold = cursor, evidence_time(threshold_at, now=observed_at)
        values = (
            None if success is None else success.epoch,
            None if success is None else success.sequence,
            None if success_at is None else success_at.isoformat(),
            None if failure is None else failure.epoch,
            None if failure is None else failure.sequence,
            None if threshold is None else threshold.isoformat(),
            identity,
        )
        if (
            self.db.execute(
                "SELECT 1 FROM telegram_ingress_watermarks WHERE identity=?", (identity,)
            ).fetchone()
            is None
        ):
            self.db.execute(
                """INSERT INTO telegram_ingress_watermarks
                (success_epoch,success_sequence,success_at,failure_witness_epoch,
                 failure_witness_sequence,failure_threshold_at,identity) VALUES (?,?,?,?,?,?,?)""",
                values,
            )
        else:
            self.db.execute(
                """UPDATE telegram_ingress_watermarks SET success_epoch=?,success_sequence=?,
                success_at=?,failure_witness_epoch=?,failure_witness_sequence=?,failure_threshold_at=?
                WHERE identity=?""",
                values,
            )
