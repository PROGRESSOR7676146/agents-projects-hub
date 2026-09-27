# ADR 0043: Bounded diagnostics for survived failures

Status: accepted
Date: 2026-09-27

## Context

Long-running components deliberately survive some failures: a health or
runtime-event write that fails inside an error path, a best-effort chat action,
telemetry, client close, or artifact cleanup. Thirty-one such handlers ended in
`except Exception: pass` or `continue`, and the package had no logging at all.
When recording a failure notice itself failed, nothing anywhere showed that it
happened.

Logging exception text is not an option. Telegram and provider errors can embed
bot-token URLs, prompts, account data or local paths, while REQ-OPS-009,
REQ-SEC-004 and AC-NF-001 forbid exception detail, paths and secrets in health
and logs.

## Decision

`diagnostic_log.survived(site, error)` records one warning containing only the
exception class name and a static `module.step` label. It never writes the
exception text, arguments or traceback. A label that does not match the static
form is replaced, so a caller cannot pass dynamic identifiers. The same site
and class are emitted at most once a minute, with a count of the suppressed
repeats.

The package logger has a `NullHandler`, so library use stays silent. The CLI
entry point attaches one stderr handler, which systemd captures in the user
journal; stdout keeps its JSON contract.

Every former silent broad handler in `src` now calls `survived`, except two JSON
log-line parsers that now catch only the parse errors they expect. Ruff rules
`S110` and `S112` reject new silent broad handlers in production code; tests
are exempt because fakes and fault injection swallow exceptions on purpose.

## Consequences

Durable health, runtime events and user-visible notices remain the
authoritative failure channels; the journal adds a bounded trace for failures
those channels could not record. Operators see the class and the step, not the
cause; exact causes still need a local reproduction or private diagnostics.
The remaining catch-all handlers that do record their failure elsewhere are
unchanged; `BLE001` is not enabled.
