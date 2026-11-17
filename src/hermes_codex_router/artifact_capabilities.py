"""Capabilities fixed in this release artifact, independent of configuration."""

REMOTE_DELIVERY_HOLD_RECOVERY = True
# ADR 0107: one-tap owner decision for an uncertain non-Codex job. Enabled by
# owner decision in 0.11.23; 0.11.22 (off, schema 57) is its rollback target.
REMOTE_UNCERTAIN_DECISION = True
