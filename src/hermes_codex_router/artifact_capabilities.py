"""Capabilities fixed in this release artifact, independent of configuration."""

REMOTE_DELIVERY_HOLD_RECOVERY = True
# ADR 0107: one-tap owner decision for an uncertain non-Codex job. Enabled by
# owner decision in 0.11.23; 0.11.22 (off, schema 57) is its rollback target.
REMOTE_UNCERTAIN_DECISION = True

# ADR 0108: the distinct schema-58 rollback artifact retains spool files while
# continuing to record exact retirement evidence.  It can therefore reopen a
# migrated database without repeating the candidate's local cleanup behavior.
# An install target removes only documents whose terminal proof committed.
RECEIPT_BOUND_ARTIFACT_CLEANUP = True

# ADR 0109: the schema-59-compatible rollback artifact keeps the immutable
# ledger reader but sets this false.  It can therefore recover an already
# migrated state without extending the owner-attested loss history.  0.11.28
# is the schema-60 rollback artifact for 0.11.29 and ships both flags off;
# 0.11.29, the install target, ships both on.
EXPLICIT_ARTIFACT_LOSS_DECLARATION = True
