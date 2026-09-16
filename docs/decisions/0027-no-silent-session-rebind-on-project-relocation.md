# ADR 0027: no silent provider-session rebind on project relocation

Status: accepted and implemented offline
Date: 2026-09-16

## Context

A Hub project registration may need a new local Git root while preserving its
immutable project ID and numeric Telegram group binding. Provider-native
sessions are not portable root-neutral records: their own metadata, tools and
execution history were created for one filesystem root. Rewriting only Hub
SQLite metadata would make the next resume claim continuity that the provider
cannot safely prove.

The registry is an atomic JSON projection while dynamic group/root receipts are
SQLite state. They cannot participate in one storage-engine transaction. A
process may therefore stop after replacing the registry but before committing
the matching dynamic receipt.

## Decision

Project relocation never rewrites a provider-native session, immutable Codex
origin, checkpoint or history record. Any non-archived session with a provider
session ID blocks relocation, as do local/terminal writer ownership,
queued/running work, pending result/progress delivery and unresolved
indeterminate work. The owner must explicitly close or archive the old session,
relocate the project, and then create or connect a session whose provider
metadata already names the new root.

Telegram selects only opaque workflow and root-option IDs. Candidates are
bounded to canonical direct children of configured allowed roots. Existing
candidates must be exact Git roots; only an empty or absent direct child named
with the immutable project ID may be initialized. Symlinks, escapes, duplicate
roots and non-empty non-Git directories fail closed.

Schema 29 persists project selection, the old registration snapshot, requested
operation, opaque options, confirmation and apply state. Apply holds an
immediate SQLite transaction and a cross-process registry lock. A handled fault
restores the old registry document and rolls back SQLite. If the process stops
after atomic registry replacement, the durable `applying` intent is recovered
before Controller admission: recovery recognizes the exact target projection,
updates only the current dynamic root receipt and completes the workflow. Any
other registry shape is a conflict, never an inferred rebind.

Display-name editing changes only `display_name`. It does not modify
`topic_name`, call a Telegram group-title API, or alter numeric bindings.
Relocation does not move, copy or delete either root, repository content, Git
history or a Telegram group.

## Consequences

Migration is deliberately two-step for the owner: archive the old provider
session, relocate, then start or attach a session at the new root. This loses no
provider history and prevents an apparently continuous thread from operating in
the wrong filesystem.

The registry replacement is the recoverable cross-store commit point. During an
unrecovered mismatch dynamic resolution fails closed. Startup recovery must run
before new Telegram work is admitted. Runtime rollback requires a distinct clean
artifact supporting schema 29; deleting workflow or immutable-origin evidence
is not a rollback strategy.

Repository tests cover fictional roots, sessions, work and crash boundaries.
They are automated offline evidence, not a live Telegram edit canary or accepted
deployment.
