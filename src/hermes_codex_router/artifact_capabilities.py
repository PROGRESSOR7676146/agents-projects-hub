"""Capabilities fixed in this release artifact, independent of configuration."""

REMOTE_DELIVERY_HOLD_RECOVERY = True
# ADR 0107: one-tap owner decision for an uncertain non-Codex job. Off in the
# release that introduces schema 57, which is the rollback target of enabling.
REMOTE_UNCERTAIN_DECISION = False
