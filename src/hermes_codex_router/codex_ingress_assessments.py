"""Exact-target poll assessments; no native, delivery or interrupt authority."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import cast

from .codex_ingress_precaution_policy import (
    IngressAssessment,
    IngressEpisode,
    IngressIdentity,
    IngressReason,
    assess_ingress,
    retain_earlier_deadline,
    valid_poll_evidence,
)
from .state_errors import StateError
from .telegram_ingress_ledger import TelegramIngressLedger
from .telegram_ingress_watermarks import (
    IngressWatermark,
    PollCursor,
    evidence_time,
    row_cursor,
)


@dataclass(frozen=True, slots=True)
class EpisodeCause:
    episode: IngressEpisode
    cutoff: PollCursor | None
    source_failure: PollCursor | None


def _prior_cause(
    row: sqlite3.Row | None, identity: IngressIdentity, *, now: datetime
) -> EpisodeCause | None:
    if row is None or row["reason"] is None:
        return None
    since = evidence_time(row["since"], now=now, future=True)
    # A deadline is intentionally in the future; it is not an observed clock.
    deadline = evidence_time(row["deadline"], now=now, future=True)
    recovery = evidence_time(row["recovery_after"], now=now)
    if since is None or deadline is None or recovery is None or deadline < since:
        raise StateError("incoherent retained ingress episode")
    return EpisodeCause(
        IngressEpisode(identity, cast(IngressReason, row["reason"]), since, deadline, recovery),
        row_cursor(row, "recovery_cutoff_epoch", "recovery_cutoff_sequence", minimum=0),
        row_cursor(row, "source_failure_epoch", "source_failure_sequence"),
    )


def _recover(cause: EpisodeCause | None, watermark: IngressWatermark) -> EpisodeCause | None:
    if (
        cause is not None
        and watermark.success is not None
        and watermark.success_at is not None
        and (cause.cutoff is None or watermark.success > cause.cutoff)
        and watermark.success_at >= cause.episode.recovery_after
    ):
        return None
    return cause


class CodexIngressAssessments:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        transaction: Callable[[], AbstractContextManager[None]],
        ledger: TelegramIngressLedger,
    ) -> None:
        self.db = connection
        self.transaction = transaction
        self.ledger = ledger

    def read(self, job_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM codex_telegram_ingress_assessments WHERE job_id=?", (job_id,)
        ).fetchone()

    def assess(self, job_id: str, *, now: datetime) -> IngressAssessment:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise StateError("ingress assessment time must be timezone-aware")
        now = now.astimezone(timezone.utc)
        with self.transaction():
            target = self.db.execute(
                """SELECT target.ingress_identity,control.accepted_at
                FROM codex_telegram_precaution_targets target
                JOIN codex_turn_controls control ON control.job_id=target.job_id
                WHERE target.job_id=? AND control.origin='accepted_v48'""",
                (job_id,),
            ).fetchone()
            if target is None:
                raise StateError("ingress assessment requires a fresh exact Telegram target")
            identity = cast(IngressIdentity, target["ingress_identity"])
            accepted_at = evidence_time(target["accepted_at"], now=now)
            assert accepted_at is not None
            prior = self.read(job_id)
            if prior is not None:
                assessed_at = evidence_time(prior["last_assessed_at"], now=now)
                if assessed_at is None or now < assessed_at:
                    raise StateError("ingress assessment clock moved backwards")
            row = self.db.execute(
                "SELECT * FROM telegram_group_ingress WHERE identity=?", (identity,)
            ).fetchone()
            cursor = None if row is None else row_cursor(row, "epoch", "poll_sequence", minimum=0)
            previous_cursor = (
                None
                if prior is None
                else row_cursor(prior, "last_read_epoch", "last_read_sequence", minimum=0)
            )
            if previous_cursor is not None and (cursor is None or cursor < previous_cursor):
                raise StateError("retained ingress ledger disappeared or regressed")
            snapshot = self.ledger.read(identity)
            if snapshot is not None and not valid_poll_evidence(snapshot.evidence, now):
                raise StateError("incoherent retained group poll evidence")
            watermark = self.ledger.watermarks.read(identity, ledger=row, now=now)
            confirmation = None if snapshot is None else snapshot.last_confirmed_poll_at
            previous_confirmation = (
                None if prior is None else evidence_time(prior["last_confirmed_poll_at"], now=now)
            )
            if confirmation is not None:
                evidence_time(confirmation.isoformat(), now=now)
            if previous_confirmation is not None and (
                confirmation is None or confirmation < previous_confirmation
            ):
                raise StateError("retained ingress confirmation regressed")
            confirmation = max(
                (value for value in (confirmation, previous_confirmation) if value is not None),
                default=None,
            )
            old_cause = _prior_cause(prior, identity, now=now)
            surviving = _recover(old_cause, watermark)
            selected = surviving
            if watermark.failure is not None:
                threshold = watermark.failure_threshold_at
                assert threshold is not None
                candidate = EpisodeCause(
                    IngressEpisode(
                        identity,
                        "poll_failures",
                        threshold,
                        threshold + timedelta(seconds=30),
                        threshold,
                    ),
                    watermark.failure,
                    watermark.failure,
                )
                if (
                    selected is None
                    or retain_earlier_deadline(selected.episode, candidate.episode)
                    is candidate.episode
                ):
                    selected = candidate
            result = assess_ingress(
                None if snapshot is None else snapshot.evidence,
                expected_identity=identity,
                accepted_at=accepted_at,
                now=now,
                prior=None if selected is None else selected.episode,
                last_confirmed_poll_at=confirmation,
            )
            # Timestamp-only success cannot retire a logically later cause, and
            # historical recovery cannot prove that the current epoch is polling.
            if selected is not None and result.episode is None:
                result = IngressAssessment(False, selected.episode, result.last_confirmed_poll_at)
            if result.episode is not None and (
                selected is None or result.episode is not selected.episode
            ):
                selected = EpisodeCause(result.episode, cursor, None)
            if result.episode is None:
                selected = None
            generation = 0 if prior is None else int(prior["episode_generation"])
            if selected is not None and surviving is None:
                generation += 1
            self._persist(job_id, now, prior, cursor, generation, selected, result)
            return result

    def _persist(
        self,
        job_id: str,
        now: datetime,
        prior: sqlite3.Row | None,
        cursor: PollCursor | None,
        generation: int,
        cause: EpisodeCause | None,
        result: IngressAssessment,
    ) -> None:
        episode = None if cause is None else cause.episode
        cutoff = None if cause is None else cause.cutoff
        failure = None if cause is None else cause.source_failure
        values = (
            1 if prior is None else int(prior["assessment_revision"]) + 1,
            now.isoformat(),
            None if cursor is None else cursor.epoch,
            None if cursor is None else cursor.sequence,
            None
            if result.last_confirmed_poll_at is None
            else result.last_confirmed_poll_at.isoformat(),
            int(result.recent_poll_confirmed),
            generation,
            None if episode is None else episode.reason,
            None if episode is None else episode.since.isoformat(),
            None if episode is None else episode.deadline.isoformat(),
            None if episode is None else episode.recovery_after.isoformat(),
            None if cutoff is None else cutoff.epoch,
            None if cutoff is None else cutoff.sequence,
            None if failure is None else failure.epoch,
            None if failure is None else failure.sequence,
            job_id,
        )
        if prior is None:
            self.db.execute(
                """INSERT INTO codex_telegram_ingress_assessments
                (assessment_revision,last_assessed_at,last_read_epoch,last_read_sequence,
                last_confirmed_poll_at,recent_poll_confirmed,episode_generation,reason,since,
                deadline,recovery_after,recovery_cutoff_epoch,recovery_cutoff_sequence,
                source_failure_epoch,source_failure_sequence,job_id,policy_version)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                values,
            )
        else:
            self.db.execute(
                """UPDATE codex_telegram_ingress_assessments SET assessment_revision=?,
                last_assessed_at=?,last_read_epoch=?,last_read_sequence=?,last_confirmed_poll_at=?,
                recent_poll_confirmed=?,episode_generation=?,reason=?,since=?,deadline=?,
                recovery_after=?,recovery_cutoff_epoch=?,recovery_cutoff_sequence=?,
                source_failure_epoch=?,source_failure_sequence=? WHERE job_id=?""",
                values,
            )
