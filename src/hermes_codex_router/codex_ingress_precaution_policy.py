"""Pure ingress policy prerequisite; no persistence, authority or provider I/O.

Evidence comes from the fenced group-controller poll ledger, never runtime_health.
Exact-target persistence lives in codex_ingress_assessments; control integration
remains pending (REQ-QUEUE-014).
Episodes passed here must belong to the caller's exact accepted control target.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

IngressIdentity = Literal["hub", "codex"]
IngressReason = Literal["never_confirmed", "stale_or_missing", "poll_failures"]


@dataclass(frozen=True, slots=True)
class IngressPollEvidence:
    identity: IngressIdentity
    epoch: int
    registered_at: datetime
    heartbeat_at: datetime
    last_poll_at: datetime | None
    last_success_at: datetime | None
    failure_streak: int
    failure_threshold_at: datetime | None


@dataclass(frozen=True, slots=True)
class IngressEpisode:
    identity: IngressIdentity
    reason: IngressReason
    since: datetime
    deadline: datetime
    recovery_after: datetime


@dataclass(frozen=True, slots=True)
class IngressAssessment:
    recent_poll_confirmed: bool
    episode: IngressEpisode | None
    last_confirmed_poll_at: datetime | None


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None
    )


def valid_poll_evidence(evidence: IngressPollEvidence, now: datetime) -> bool:
    if (
        evidence.identity not in {"hub", "codex"}
        or type(evidence.epoch) is not int
        or evidence.epoch < 1
        or type(evidence.failure_streak) is not int
        or evidence.failure_streak < 0
    ):
        return False
    dates = (evidence.registered_at, evidence.heartbeat_at)
    optional = (evidence.last_poll_at, evidence.last_success_at, evidence.failure_threshold_at)
    present = (*dates, *(value for value in optional if value is not None))
    if any(not _aware(value) for value in present):
        return False
    if any(value > now + timedelta(seconds=5) for value in present):
        return False
    if any(value < evidence.registered_at or value > evidence.heartbeat_at for value in present):
        return False
    if evidence.last_success_at is not None and (
        evidence.last_poll_at is None or evidence.last_success_at > evidence.last_poll_at
    ):
        return False
    if evidence.failure_streak >= 3:
        threshold = evidence.failure_threshold_at
        if threshold is None or evidence.last_poll_at is None or threshold > evidence.last_poll_at:
            return False
        if evidence.last_success_at is not None and threshold < evidence.last_success_at:
            return False
    elif evidence.failure_threshold_at is not None:
        return False
    return True


def _validate_episode(prior: IngressEpisode) -> None:
    if (
        prior.identity not in {"hub", "codex"}
        or prior.reason not in {"never_confirmed", "stale_or_missing", "poll_failures"}
        or any(not _aware(value) for value in (prior.since, prior.deadline, prior.recovery_after))
        or prior.deadline < prior.since
    ):
        raise ValueError("invalid retained ingress episode")


def retain_earlier_deadline(
    prior: IngressEpisode | None, candidate: IngressEpisode
) -> IngressEpisode:
    """A classification change or restart cannot extend a pending deadline."""
    _validate_episode(candidate)
    if prior is None:
        return candidate
    _validate_episode(prior)
    if prior.identity != candidate.identity:
        raise ValueError("ingress episode identity changed")
    return prior if prior.deadline <= candidate.deadline else candidate


def assess_ingress(
    evidence: IngressPollEvidence | None,
    *,
    expected_identity: IngressIdentity,
    accepted_at: datetime,
    now: datetime,
    prior: IngressEpisode | None = None,
    last_confirmed_poll_at: datetime | None = None,
) -> IngressAssessment:
    """Classify poll uncertainty, without claiming exact-topic controllability.

    The state owner must retain the episode and recheck evidence/consent in
    the same transaction as the existing exact-turn send fence. This function
    alone cannot authorize an interrupt or clear an unknown delivery/sender.
    """
    if expected_identity not in {"hub", "codex"} or not _aware(accepted_at) or not _aware(now):
        raise ValueError("invalid accepted target clock or ingress identity")
    accepted_at, now = accepted_at.astimezone(timezone.utc), now.astimezone(timezone.utc)
    if accepted_at > now + timedelta(seconds=5):
        raise ValueError("accepted target clock is in the future")
    if prior is not None:
        _validate_episode(prior)
        if prior.recovery_after > now + timedelta(seconds=5):
            raise ValueError("retained ingress recovery clock is in the future")
        if prior.identity != expected_identity:
            raise ValueError("retained ingress episode belongs to another identity")
    if last_confirmed_poll_at is not None:
        if not _aware(last_confirmed_poll_at) or last_confirmed_poll_at > now + timedelta(
            seconds=5
        ):
            raise ValueError("invalid retained ingress confirmation")
        last_confirmed_poll_at = last_confirmed_poll_at.astimezone(timezone.utc)
    usable = (
        evidence is not None
        and evidence.identity == expected_identity
        and valid_poll_evidence(evidence, now)
    )
    if usable:
        assert evidence is not None
        if evidence.last_success_at is not None:
            last_confirmed_poll_at = max(
                value
                for value in (last_confirmed_poll_at, evidence.last_success_at)
                if value is not None
            )
    since = accepted_at
    reason: IngressReason = "never_confirmed"
    if last_confirmed_poll_at is not None:
        since = max(accepted_at, last_confirmed_poll_at + timedelta(seconds=60))
        reason = "stale_or_missing"
    candidate = IngressEpisode(
        expected_identity, reason, since, since + timedelta(seconds=120), now
    )
    if not usable:
        return IngressAssessment(
            False, retain_earlier_deadline(prior, candidate), last_confirmed_poll_at
        )
    assert evidence is not None
    recent_heartbeat = now - evidence.heartbeat_at <= timedelta(seconds=60)
    recent_poll = evidence.last_poll_at is not None and now - evidence.last_poll_at <= timedelta(
        seconds=60
    )
    recent_success = (
        evidence.last_success_at is not None
        and now - evidence.last_success_at <= timedelta(seconds=60)
    )
    if recent_heartbeat and recent_poll and recent_success and evidence.failure_streak < 3:
        assert evidence.last_success_at is not None
        if prior is None or evidence.last_success_at >= prior.recovery_after:
            return IngressAssessment(True, None, last_confirmed_poll_at)
        return IngressAssessment(False, prior, last_confirmed_poll_at)
    if recent_heartbeat and recent_poll and evidence.failure_streak >= 3:
        assert evidence.failure_threshold_at is not None
        since = evidence.failure_threshold_at
        candidate = IngressEpisode(
            expected_identity, "poll_failures", since, since + timedelta(seconds=30), since
        )
    return IngressAssessment(
        False, retain_earlier_deadline(prior, candidate), last_confirmed_poll_at
    )
