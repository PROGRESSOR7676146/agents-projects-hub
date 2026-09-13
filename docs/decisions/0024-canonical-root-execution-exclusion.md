# ADR 0024: Canonical-root execution exclusion

Status: repository implementation; deployment acceptance pending
Date: 2026-09-13

## Decision

Schema 26 gives every Telegram topic a durable `execution_scope`. Current Hub
ingress binds that scope to the registered canonical project root. Queue leases,
local/terminal writer transfer, and saved Codex-session adoption use the scope
inside the same `BEGIN IMMEDIATE` transaction that changes ownership. At most one
Hub-owned productive writer may therefore own a canonical root across different
topics and providers.

The queue retains strict FIFO within each topic, but a blocked root does not
prevent selection of eligible work for another scope. A separately registered
Git worktree has a different canonical root and may proceed independently; a
different Telegram topic or project ID alone does not create an independent
lane. This is an exclusion foundation, not an increase in worker count or a
general-purpose lane scheduler.

An unexpired lease owns the scope before provider invocation. An expired lease
that never entered `executing` releases it and its old token cannot start work.
An expired `executing` job becomes `indeterminate` and retains the scope.
Appending the existing immutable operator resolution releases the scope only
for future work: it does not change the old job, infer an outcome, deliver a
result, or authorize replay.

The guarantee covers productive execution coordinated by one Hub SQLite state:
embedded/external queue workers and Hub local ownership. Provider direct-message
services use separate legacy state databases, and independently managed Hermes
or native CLI processes are outside this coordination boundary.

## Alternatives rejected

- A global worker mutex would serialize unrelated project roots and turn one
  stalled provider into system-wide head-of-line blocking.
- Topic-only exclusion permits two topics to modify the same checkout.
- Project-ID exclusion fails when a canonical root is re-registered under a new
  immutable ID.
- Automatically releasing uncertain execution after timeout would permit two
  writers when the first provider actually continued after Hub lost contact.
- Treating the existing `worktree_lanes` metadata as execution isolation would
  be unsafe because it does not itself change or validate a worker's cwd.

## Migration and rollback

Migration 26 adds the nullable column transactionally, backfills existing rows
to `project:<project_id>`, and adds a lookup index. The next normal observation
through a registry-aware Hub path upgrades that fallback to
`root:<canonical-root>`. A conflicting later root is rejected. Rollback requires
an artifact whose maximum supported schema is at least 26; retaining the column
and its values is safer than a destructive downgrade.

## Evidence

Offline tests cover atomic competing worker claims, same-root cross-provider
serialization, local-transfer/lease races, project-ID aliases of one canonical
root, independent roots, pre-execution lease expiry with late-token rejection,
real worker loops, crash-to-indeterminate recovery, resolution release, adoption
conflicts, migration backfill, and DDL rollback. These tests do not constitute
deployment or live provider acceptance.
