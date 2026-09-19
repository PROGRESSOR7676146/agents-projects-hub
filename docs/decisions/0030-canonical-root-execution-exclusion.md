# ADR 0030: Canonical-root execution exclusion

Status: repository implementation; deployment acceptance pending
Date: 2026-09-13

## Decision

Schema 31 gives every Telegram topic a durable `execution_scope`. Current Hub
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

The unmerged development branch used earlier migration/ADR numbers. Integration
reserves released schemas 26–30 for connect, provisioning, editing and provider
provenance; this decision uses an additive migration after schema 30. Released
migrations are unchanged. A prerelease database from the divergent branch is
not a supported released-schema input and must not be relabelled to force an upgrade.

Migration 31 adds the nullable column transactionally, backfills existing rows
to `project:<project_id>`, and adds a lookup index. On Controller, worker, and
standalone external-service startup (including each separate direct-message
database), a registry-aware transaction upgrades every retained non-lane fallback
to `root:<canonical-root>` using retained origin/checkpoint identity first and
the registry only when no such evidence exists. This
prevents a worker started before the Controller from comparing a new canonical
scope to a retained legacy string. Active lane scopes are preserved and a
mismatched lane binding fails closed; a stored canonical scope is never rebound
only because the registry later changes. A single saved root protects the same
historical checkout even if a known ID now maps elsewhere; execution and topic
observation refuse the mismatched binding, while independent roots can proceed.
This is not authorization to execute the saved path without registry/Git
validation. An unknown historical ID is normalized only from one retained
origin/checkpoint root. Multiple saved roots, or unknown active ownership with
no root evidence, fail the entire normalization transaction for local resolution
rather than guessing from titles or paths. Checkpoints, origins, writer modes,
jobs, resolutions and outbox records are unchanged. Startup refusal closes its
SQLite connection. The procedure also applies to already-schema-32 databases;
no historical migration is rewritten. Null and empty fallback scopes are matched
against their actual stored value using a null-safe comparison, without bypassing
the same evidence and active-lane checks. Rollback
requires an artifact whose maximum supported schema is at least 31; retaining
the column and its values is safer than a destructive downgrade.

## Managed terminal ownership

Inline managed terminal takeover validates the root outside SQLite, then claims
terminal ownership with the same persisted topic/session/lane snapshot check as
local transfer, before provider-session preparation or process launch. The claim
excludes other Hub writers while launch is pending. Preparation or launch failure
retains ownership conservatively; neither a repeated takeover nor a negative
process-liveness observation authorizes another launch or a Telegram turn. The
operator inspects locally and explicitly uses `/release` to return ownership.
No provider or filesystem operation runs inside the ownership transaction.

## Evidence

Offline tests cover atomic competing worker claims, same-root cross-provider
serialization, local-transfer/lease races, project-ID aliases of one canonical
root, independent roots, pre-execution lease expiry with late-token rejection,
real worker loops, crash-to-indeterminate recovery, resolution release, adoption
conflicts, migration backfill, and DDL rollback. These tests do not constitute
deployment or live provider acceptance.
