# Exact-job outcome diagnostics

This read-only slice supports the private outcome journal in
[REQ-EVAL-010](../product/EVALUATION_AND_ALLOCATION.md); it does not complete the
lead/advisor milestone or provide owner acceptance decisions.

```bash
agents-projects-hub outcome-journal /home/example/.config/agents-projects-hub/hub.json EXAMPLE_JOB_ID
```

The command reads one existing job from a current-schema database. It prints a
versioned JSON diagnostic to local stdout, without creating or migrating state,
reading Telegram credentials, opening artifact files, invoking providers,
publishing to Telegram, or authorizing replay. It creates no export file.
SQLite may create its ordinary WAL coordination files when opening a quiescent
WAL database, and those files may persist until a later writable connection
cleans them up. The command does not change database rows, schema or mode.
Passive configuration validation can also open the same state read-only to check
native-origin compatibility before HubState opens its query-only connection.
Treat this local output as private operator data. Unsupported state or SQLite
JSON support returns a fixed error code; there is no writable fallback.
Malformed stored types can make the complete projection unavailable instead of
yielding partial fields. A missing topic is reported as a missing job, and a
missing job takes precedence over a concurrent schema mismatch in this slice.

The report preserves the requested model/effort from the queued job. A stored
result model label can include a requested-model fallback, so observed model,
effort and historical runtime remain unknown. Provider success, saved completion,
terminal observations, delivery and historical indeterminate-work resolution are
separate facts; none establishes owner acceptance. Usage and monetary cost remain
unknown because existing durable job records do not prove them.

Schema43 additionally reports validated receipt counts and whole-part provenance.
Positive legacy IDs remain historical receipts with unverified provenance.
Resultless failure/uncertainty notices have a separate `notice_delivery` object;
even after independent native terminal proof they cannot establish final-result
delivery. Unknown delivery remains visible and authorizes neither resend nor
provider replay.

Timing endpoints identify their source columns. Admission-to-result-commit and
latest-worker-phase-to-result-commit are wall-clock observations: preparation is
included, and recovery can delay result persistence. They are not native execution,
queue or approval durations. A delivery interval requires a result-bound outbox,
matching sender/destination, delivered status and receipts for all parts. Missing,
invalid or timezone-free endpoints yield unknown intervals; reversed endpoints
have a separate reason. A saved native turn ID is execution evidence only.
Zero-part legacy deliveries cannot establish a delivery interval. Unconfirmed
delivery and invalid delivery timestamps share the interval's unknown reason;
the separate delivery status, part counts and receipt flag retain those facts.

Artifact references include outbox/part identity, size, digest and receipt presence.
They do not establish present availability: delivered spool files may have been
removed. Artifact and direct lineage pages contain at most 64 entries each, with
the full matching count and an explicit truncation flag. Retries, continuations
and absorbed input stay distinct; the projection never copies a parent's result
or sums consumption. One SQL statement keeps all fields in one read snapshot.
Page ordering is normalized after decoding; receipt presence is a JSON boolean.
Non-ASCII output is JSON-escaped to prevent control-character display spoofing.

No prompts, visible or partial response text, raw metadata, error detail, leases,
native session IDs, roots, file paths or names, bot/account identities or notice
contents enter this projection. The schema version is checked again within the
projection's read snapshot. A failed or closed stdout returns nonzero without
attempting a second error document; a partially written JSON cannot be retracted.
Python may retry a buffered flush during interpreter shutdown, emitting its own
stderr diagnostic or changing the nonzero exit status. Argument-parser failures
occur before this command's sanitized JSON handler and use ordinary argparse
stderr/exit behavior; interrupts and other BaseException cases are also outside
that handler. Stored model labels are bounded, JSON-escaped provider text, not
validated model identities or verified observations.

Focused offline checks:

```bash
python scripts/validate.py --profile focused tests.test_outcome_journal
```

Fixtures exercise immutable selection, unknown acceptance/usage, failed notices,
artifact/lineage bounds, delivery ownership/receipts, empty completion, invalid
time, concurrent writes, read-only state opening and sanitized command errors.
They do not establish deployment, Telegram acceptance or subscription routing.
Authoritative immutable owner decisions, corrections, per-job observation
provenance and the collaboration workflow remain separate follow-ups.
