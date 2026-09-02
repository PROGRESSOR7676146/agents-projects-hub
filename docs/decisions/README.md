# Decision map

Status: active  
Last updated: 2026-08-30

This directory is the durable entry point for consequential product and
architecture decisions. New records should be named `NNNN-short-title.md`.
Accepted records are not rewritten to hide a changed decision; supersede them
with a new record.

## Accepted decisions already established

| Decision | Current outcome | Source |
| --- | --- | --- |
| Product identity | Agents Projects Hub; internal `hermes_codex_router` remains for compatibility. | Product requirements |
| Project topology | One private forum group per registered local project; numeric topic identity. | Product requirements and tests |
| Routing | One central ingress; ordinary → active, Reply → author, mention → target, selected/pasted quote → active. | Product requirements; tests and commit history |
| Context | Bounded completed visible turns are delivered on the next productive turn; passive observation does not call models. | Product requirements; tests and commit history |
| Provider identity | One bot identity per runtime, not per model or account. | Product requirements and tests |
| Codex accounts | Multi-auth is optional; official Codex app-server is the fallback. | Product requirements; `RECOVERY_PLANE.ru.md` |
| Headless Codex approvals | Shared sockets use companion-backed `on-request`; isolated stdio fallback denies escalation instead of waiting on an unreachable approval channel. | [ADR 0006](0006-headless-codex-fallback-approvals.md) |
| Recovery plane | Hermes Gateway and Agent Session Remote/tlive are independent service channels, not project groups or mandatory Hub dependencies. | Product requirements; `RECOVERY_PLANE.ru.md` |
| Operational alerts | One explicit Hub Operations/Alerts topic; Codex primary, Hermes fallback to the same topic; masked account hints. | Product requirements REQ-OPS-006 |
| Local frontend | Native CLI is preferred; one-writer lease is mandatory; tmux remains fallback. | Product requirements and tests |
| Publication privacy | Deployment identities and live transcripts remain outside Git; automated privacy scan is mandatory. | Product requirements and security policy |
| Durable execution isolation | Planned deterministic Controller, SQLite queue, strict topic FIFO, isolated provider workers, and outbox; unknown in-flight turns become `indeterminate`, not automatic retries. | [ADR 0001](0001-durable-provider-job-queue.md) |
| Live input semantics | Durable burst collection, capability-aware Codex steering, FIFO fallback, and model-free emergency stop. | [ADR 0004](0004-durable-input-batching-steering-and-stop.md) |
| Runtime health | Components publish bounded last-known state to SQLite; status classifies the cache without provider or model calls. | [ADR 0002](0002-durable-runtime-health-cache.md) |
| Telegram interaction | Providers own conversational meaning; the Hub owns Telegram UI effects and delivery guarantees. | [ADR 0005](0005-telegram-interaction-contract.md) |

The table is an index, not a substitute for the normative product requirements.
Create an individual decision record when a future change supersedes any row or
introduces a durable trade-off whose rationale must outlive its implementation.
