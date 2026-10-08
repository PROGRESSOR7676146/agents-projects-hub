# Exact-job outcome diagnostics

This diagnostic and the bounded owner-command slice support the private outcome
journal in [REQ-EVAL-010](../product/EVALUATION_AND_ALLOCATION.md). They do not
complete the lead/advisor milestone, usage provenance or deployed custody.

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

Schema45 projects the latest applied owner disposition separately, with its
reason, revision, actor, input identity and recording time. An explicit owner
`unknown` includes that provenance; absence of a decision remains unknown with
no authoritative source. Refused commands do not replace acceptance. Human
reasons are private owner-supplied text, JSON-escaped in local output; they are
not provider diagnostics, verified facts or future task instructions.

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
Per-job observation provenance and the collaboration workflow remain separate
follow-ups.

## Owner decision procedure

In the registered project topic, Reply to a saved final part with:

```text
/assess accepted Checked the example result
/assess rework The example output needs correction
/assess unknown Verification is incomplete
```

For a correction, Reply to your latest applied `/assess` message. The Hub's
acknowledgement is not the correction target. A refusal asks for a fresh command;
retrying its old message after delivery completes retains that original refusal.
Forwarded commands remain passive context; selected quotes, captions and
attachments are refused without download. No public-menu entry is added.
The supported path requires central Hub ingress and external queue/outbox.

The independent sender delivers acknowledgements under Hub identity. Unknown
acknowledgement delivery does not erase the recorded decision or permit resend;
correction still targets the human command. Inspect the exact job through
`outcome-journal` to see its latest applied assessment. `rework` records a
decision only; send a separate explicit task to authorize further work.
An assessment closes existing queued future deadlines in that topic as a normal
command boundary. Holds and `retry_wait` states remain intact, and a duplicate
does not flush newly admitted work. This has no provider invocation or writer
transfer in the command handler.

Migration45 rebuilds notices and legacy stop links in one migration transaction;
retain a schema44 backup and schema45-compatible rollback runtime. Source tests
cover all notice states, positive receipts, foreign keys, DDL rollback,
two-connection first/correction races, commit and notice faults, unchanged
execution evidence and unknown acknowledgement after restart. Activation and
Telegram acceptance require separate authorization on the exact reviewed release,
including proof that productive models cannot modify authoritative journal data.
