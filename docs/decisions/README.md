# Decision map

Status: active  
Last updated: 2026-08-29

This directory is the durable entry point for consequential product and
architecture decisions. New records should be named `NNNN-short-title.md`.
Accepted records are not rewritten to hide a changed decision; supersede them
with a new record.

## Accepted decisions already established

| Decision | Current outcome | Source |
| --- | --- | --- |
| Product identity | Agents Projects Hub; Hub is the project-group display name; internal `hermes_codex_router` remains for compatibility. | Product requirements and 2026-08-29 handoff |
| Project topology | One private forum group per registered local project; numeric topic identity. | Product requirements; `PROJECT_HUB_SPEC.ru.md` |
| Routing | One central ingress; ordinary → active, Reply → author, mention → target, selected/pasted quote → active. | Product requirements; tests and commit history |
| Context | Bounded completed visible turns are delivered on the next productive turn; passive observation does not call models. | Product requirements; tests and commit history |
| Provider identity | One bot identity per runtime, not per model or account. | Product requirements and sanitized history |
| Codex accounts | Multi-auth is optional; official Codex app-server is the fallback. | Product requirements; `RECOVERY_PLANE.ru.md` |
| Recovery plane | Hermes Gateway and Agent Session Remote/tlive are independent service channels, not project groups or mandatory Hub dependencies. | Product requirements; `RECOVERY_PLANE.ru.md` |
| Operational alerts | One explicit Hub Operations/Alerts topic; Codex primary, Hermes fallback to the same topic; masked account hints. | Product requirements REQ-OPS-006 |
| Local frontend | Native CLI is preferred; one-writer lease is mandatory; tmux remains fallback. | Product requirements; `PROJECT_HUB_SPEC.ru.md` |
| Near-term delivery | Complete live E2E, then minimal `/local`/`/return`, bounded `/publish`, and encrypted recovery. | Product requirements and latest handoff |

The table is an index, not a substitute for the normative product requirements.
Create an individual decision record when a future change supersedes any row or
introduces a durable trade-off whose rationale must outlive its implementation.
