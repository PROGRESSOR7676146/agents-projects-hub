"""State-domain ledger for actual group polls; never sends or controls anything."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timezone

from .codex_ingress_precaution_policy import IngressIdentity, IngressPollEvidence
from .state_errors import StateError


@dataclass(frozen=True, slots=True)
class IngressPollOwner:
    identity: IngressIdentity
    epoch: int
    instance_token: str


@dataclass(frozen=True, slots=True)
class IngressPollSnapshot:
    evidence: IngressPollEvidence
    last_confirmed_poll_at: datetime | None


class PollSampleRefused(StateError):
    """Rejected sample, while its instance still owns the current epoch."""


class IngressOwnershipLost(StateError):
    """The captured startup CAS no longer identifies this instance's epoch."""


def _clock(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StateError("group ingress time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat()


def _integer(value: int, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value < 2**63:
        raise StateError("invalid group ingress epoch or sequence")


def _identity(identity: str) -> IngressIdentity:
    if identity == "hub":
        return "hub"
    if identity == "codex":
        return "codex"
    raise StateError("unsupported group ingress identity")


def _token_hash(token: str) -> str:
    if not isinstance(token, str) or not 16 <= len(token) <= 128 or not token.isascii():
        raise StateError("invalid group ingress instance token")
    return hashlib.sha256(token.encode()).hexdigest()


class TelegramIngressLedger:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction: Callable[[], AbstractContextManager[None]],
    ) -> None:
        self.db = connection
        self.transaction = transaction

    def current_epoch(self, identity: str) -> int:
        row = self.db.execute(
            "SELECT epoch FROM telegram_group_ingress WHERE identity=?", (_identity(identity),)
        ).fetchone()
        return 0 if row is None else int(row[0])

    def register(
        self, identity: str, *, instance_token: str, previous_epoch: int, now: datetime
    ) -> IngressPollOwner:
        """Startup CAS only. An exact-token repeat neither bumps nor resets epoch.

        The controller captures previous_epoch once before this call. A stale
        CAS must stop publishing, never refresh that value to reclaim ownership.
        """
        identity = _identity(identity)
        _integer(previous_epoch)
        digest = _token_hash(instance_token)
        timestamp = _clock(now)
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM telegram_group_ingress WHERE identity=?", (identity,)
            ).fetchone()
            if row is not None and row["instance_token_hash"] == digest:
                if row["epoch"] != previous_epoch + 1:
                    raise IngressOwnershipLost(
                        "group ingress registration does not match original CAS"
                    )
                return IngressPollOwner(identity, int(row["epoch"]), instance_token)
            if (
                0 if row is None else row["epoch"]
            ) != previous_epoch or previous_epoch == 2**63 - 1:
                raise IngressOwnershipLost("group ingress startup epoch changed")
            if row is not None and timestamp < (row["last_poll_at"] or row["registered_at"]):
                raise StateError("group ingress startup clock moved backwards")
            epoch = previous_epoch + 1
            if row is None:
                self.db.execute(
                    """INSERT INTO telegram_group_ingress
                    (identity,epoch,instance_token_hash,registered_at,poll_sequence,failure_streak)
                    VALUES (?,?,?,?,0,0)""",
                    (identity, epoch, digest, timestamp),
                )
            else:
                self.db.execute(
                    """UPDATE telegram_group_ingress SET epoch=?,instance_token_hash=?,
                    registered_at=?,poll_sequence=0,last_poll_at=NULL,last_poll_succeeded=NULL,
                    last_success_at=NULL,failure_streak=0,failure_threshold_at=NULL WHERE identity=?""",
                    (epoch, digest, timestamp, identity),
                )
            return IngressPollOwner(identity, epoch, instance_token)

    def record_poll(
        self, owner: IngressPollOwner, *, sequence: int, succeeded: bool, observed_at: datetime
    ) -> bool:
        """Exact repeats are idempotent; sequence gaps break failure consecutiveness.

        A false result means lost instance ownership, never a request to
        register again. No error payload, transport URL or credentials enter SQL.
        """
        identity = _identity(owner.identity)
        _integer(owner.epoch, minimum=1)
        _integer(sequence, minimum=1)
        digest = _token_hash(owner.instance_token)
        if type(succeeded) is not bool:
            raise StateError("invalid group ingress poll result")
        timestamp = _clock(observed_at)
        with self.transaction():
            row = self.db.execute(
                "SELECT * FROM telegram_group_ingress WHERE identity=?", (identity,)
            ).fetchone()
            if row is None or row["epoch"] != owner.epoch or row["instance_token_hash"] != digest:
                return False
            if sequence == row["poll_sequence"]:
                if (
                    timestamp == row["last_poll_at"]
                    and int(succeeded) == row["last_poll_succeeded"]
                ):
                    return True
                raise PollSampleRefused("conflicting group ingress sample repeat")
            if sequence < row["poll_sequence"] or timestamp < (
                row["last_poll_at"] or row["registered_at"]
            ):
                raise PollSampleRefused("group ingress sample clock or sequence moved backwards")
            streak = (
                min(int(row["failure_streak"]) + 1, 1000000)
                if sequence == row["poll_sequence"] + 1
                else 1
            )
            streak = 0 if succeeded else streak
            threshold = None if streak < 3 else row["failure_threshold_at"] or timestamp
            success = timestamp if succeeded else row["last_success_at"]
            confirmed = timestamp if succeeded else row["last_confirmed_poll_at"]
            self.db.execute(
                """UPDATE telegram_group_ingress SET poll_sequence=?,last_poll_at=?,
                last_poll_succeeded=?,last_success_at=?,last_confirmed_poll_at=?,failure_streak=?,
                failure_threshold_at=? WHERE identity=? AND epoch=? AND instance_token_hash=?""",
                (
                    sequence,
                    timestamp,
                    int(succeeded),
                    success,
                    confirmed,
                    streak,
                    threshold,
                    identity,
                    owner.epoch,
                    digest,
                ),
            )
            return True

    def read(self, identity: str) -> IngressPollSnapshot | None:
        identity = _identity(identity)
        row = self.db.execute(
            "SELECT * FROM telegram_group_ingress WHERE identity=?", (identity,)
        ).fetchone()
        if row is None:
            return None

        def date(name: str) -> datetime | None:
            value = row[name]
            return None if value is None else datetime.fromisoformat(value)

        registered = date("registered_at")
        assert registered is not None
        return IngressPollSnapshot(
            IngressPollEvidence(
                identity=identity,
                epoch=int(row["epoch"]),
                registered_at=registered,
                heartbeat_at=date("last_poll_at") or registered,
                last_poll_at=date("last_poll_at"),
                last_success_at=date("last_success_at"),
                failure_streak=int(row["failure_streak"]),
                failure_threshold_at=date("failure_threshold_at"),
            ),
            date("last_confirmed_poll_at"),
        )
