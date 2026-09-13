# ADR 0022: Explicit adoption of saved Codex threads

Status: accepted and implemented; live acceptance pending
Date: 2026-09-12

## Decision

The local `session attach-codex` command connects an exact persisted Codex thread
to an existing registered topic. Preview is the default. Apply requires the
owner's explicit assertion that the native CLI is closed. Hub does not locate,
kill, or prove the absence of an unrelated CLI process. Attachment does not call
a model, send Telegram messages, import transcripts, or resume a thread.

Only external Codex queue execution with an external outbox is supported.
The registered root must be an exact canonical allowlisted Git root. Lane topics,
unavailable or unsupported metadata, known concurrent work, local writers,
unfinished deliveries and unresolved uncertain work block attachment.

Schema 25 records immutable origins and reserves their provider thread IDs even
after archival. One immediate SQLite transaction rechecks the preview snapshot,
archives the previous Hub binding when applicable, creates a fresh Hub session
and generation, binds the exact thread and assigns the local writer. A pristine
placeholder also gets a fresh identity: reusing it would admit an old ingress
snapshot with an empty provider ID into a different conversation.

Replacement requires `--replace-session EXPECTED_HUB_SESSION_ID`. It archives
only the old Hub binding. Provider threads, results and visible journal are not
deleted or merged; idle satellite sessions remain. Exact retries preserve the
binding and writer, including after `/return`. Superseded origins cannot be
resurrected by repeating an earlier command.

## Activation and control boundaries

The first typed `/return` atomically records its positive Telegram message ID,
the current forwarded-journal floor, a receipt and Telegram ownership. Queue
admission and batch append reject productive message IDs at or below this
boundary. Delayed quotes carry their source message ID, so later journal
insertion cannot bypass the boundary. Explicit `/context` remains an intentional
request for visible topic history. Subsequent local intervals do not reset the
original boundary.

Menus in topics retaining origins carry a bounded session stamp. It identifies
a generation, not authentication authority. Owner authorization and catalog
validation remain mandatory; mutating callbacks also compare the active session
inside the state transaction. Old Reply routing selects the current provider
binding, not the archived generation. A Return button asks for a typed command
while an adopted session is local; synthetic callback IDs cannot define a safe
message boundary.

Model/effort changes update the same adopted Hub session, preserving its origin
and thread. Provider switching retains it as a satellite. Explicit `/new` creates
a normal new conversation and retains the old reservation. This avoids creating
a new conversation for a settings change or adding another lineage mechanism.

## Execution and failure

Before resume overrides, the worker validates immutable origin, current binding,
allowlisted Git root and stored metadata. It verifies the resume response too.
Socket and stdio fallback must resume the exact adopted thread. Failure never
starts/forks a replacement thread or substitutes a Hub summary. Existing
Hub-created fallback behavior is unchanged. Accepted or ambiguous turns retain
the execution journal's no-replay policy. Inline/embedded Codex and pilot paths
fail closed while origins remain, including archived origins.

The inspector owns only its client and any temporary stdio child. Handshake and
metadata read share a deadline; cleanup covers connection failure too. Errors
expose fixed reason codes, not provider payloads. Configuration and cached model
choices require no bot-token reads or model discovery. CLI MCP/plugins/tools and
environment parity are not promised: Hub execution settings remain authoritative.

## Protocol evidence

The installed Codex CLI 0.154.0 JSON schema was generated offline and reviewed
alongside the [official app-server documentation](https://learn.chatgpt.com/docs/app-server).
`thread/read` with `includeTurns=false` returns metadata without loading or
resuming the thread. `thread.id`, not the distinct session-tree `sessionId`, is
the identity used here. Unknown, nonpersistent or busy sources fail closed. This
is protocol-contract evidence, not live continuity acceptance.

## Migration, rollback and evidence

The additive schema introduces origins and nullable forwarded-message provenance;
existing history is preserved and not classified as adopted. Faulted DDL rolls
back to schema 24. Executables supporting at most schema 24 reject schema 25.
Never delete origins or relabel the schema to make an old executable run.

Schema compatibility is necessary but insufficient for rollback. An actual
rollback artifact must also preserve reservation, exact resume and mode guards.
Tests exercise distinct fictional policy-aware artifacts and a refusing
schema-24 fixture; no production rollback wheel or deployment is accepted here.

Focused tests cover metadata, deadlines, cleanup, the real CLI loader, atomic
attach/activation faults, competing connections, stale controls/input, history
filtering, exact resume, uncertain acceptance, migration and rollback policy.
The offline journey uses real ingress, SQLite, worker and outbox with fictional
Codex/Telegram boundaries, across attach/replace and both transport modes,
including local continuation and reopening state.

See the [session transfer plan](../operations/SESSION_TRANSFER_IMPLEMENTATION_PLAN.md)
for implementation and acceptance navigation.
