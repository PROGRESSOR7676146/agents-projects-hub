# ADR 0025: provisioning fencing and owner membership

Status: accepted and implemented offline  
Date: 2026-09-13

## Context

The first project-group implementation established a safe user-authorized
creation boundary, but its offline review exposed gaps at restart and
multi-owner boundaries. A long RPC could outlive the fixed SQLite lease; a
second process could reuse the Telethon session; client retries could repeat a
create request; newly added projects were invisible to already running workers;
and the workflow added bots without ensuring that the other configured human
owner received equivalent day-to-day administration.

Command-scope convergence and completion notices also lacked a durable retry
contract. A sender restart could forget completed scopes, one bad group could
delay every group, and an invalid send result could be recorded with a fabricated
message ID.

## Decision

Schema 28 adds a confirmation-time snapshot of all configured owner IDs, an
explicit recoverable stage, and one durable command-scope state machine per bot
and dynamic group. The pinned technical owner creates the group. Before creation, the worker
resolves every captured owner and bot through its authorized user session. After
creation it invites every non-creator owner, grants a fixed administrator-rights
set, adds the bots, and verifies the forum, membership and rights by readback.
Changing the configured owner set blocks an already confirmed workflow until an
explicit local resume records a fresh snapshot.

One private nonblocking file lock covers login and the full provisioner lifetime.
The Telethon client disables request retry and automatic reconnect, bounds every
RPC, and closes its transport after an ambiguous mutation. SQLite leases carry
tokens and expiry checks and are refreshed during long operations. A stop before
the next Telegram RPC releases the workflow without starting that RPC. The
session lock remains the cross-process fence if a paused process outlives its
database lease.

One resolver now reloads the registry and verifies the immutable numeric binding,
dynamic root receipt, allowlisted canonical root and real Git toplevel before
Controller routing, provider execution, recovery, session adoption and command
audit. External workers perform this check while work is still leased; failure
atomically records a terminal notice without invoking a provider.

The sender gives final results priority and performs exactly one set or verify
API operation for one durable project-command task per cycle, with a command
quota after ten continuous final deliveries. Telegram cooldowns are persisted
per bot identity and also defer tasks created for new bindings during the
cooldown. Set and verify have separate attempt budgets plus one bounded total
budget, so repeated set/readback mismatch cannot loop forever; an exhausted task
requires a confirmed local reset. Read-only command
audit never creates or migrates state and reports bad groups without hiding
healthy ones. Idempotent replay is safe after a crash. Onboarding result delivery records only
a positive real Telegram message ID. Proven no-delivery failures can retry or
fail; ambiguous transport or response outcomes remain unknown.

## Consequences

The deployment still uses two human accounts: the primary owner and one
technical owner whose session is stored locally. Telegram's creator role cannot
be duplicated, so equal operation means the primary owner receives the verified
administrator rights Telegram permits while the technical account remains
creator. No primary-owner credentials are stored by Hub.

A blocked resumable workflow keeps its project ID and canonical-root reservation
until it completes or is explicitly reconciled. Existing-group shape and creator
identity are verified before the first invite or promotion, and stop/lease guards
run before every Telegram RPC in a mutation sequence.

Live acceptance is owner-driven because this two-account topology has no
independent non-administrator acceptance actor. Reports must name that boundary
and must not present it as independent unprivileged-user E2E. Runtime rollback
after migration requires a distinct clean artifact that supports schema 28.
