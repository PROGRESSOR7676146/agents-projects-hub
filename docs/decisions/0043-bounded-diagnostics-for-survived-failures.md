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

`diagnostic_log.survived(site, error)` records one warning containing only a
registered site label and an exception class name. It never writes the
exception text, arguments or traceback. Labels come from a closed registry
that a test keeps equal to the literal call sites; any other label is replaced
by a neutral value. The class name comes from a closed registry too: every
exception class of the interpreter, taken when the module is imported, plus an
explicit list of standard-library, dependency and package exceptions
(`NAMED_ERRORS`). A record names the nearest class of the exception, in method
resolution order, whose module and qualified name are exact strings matching a
registry entry, and it writes the registry's own string, never an attribute of
the class. A class built at run time, even one registered in a module under its
own name or carrying string subclasses with their own hashing, equality or
text, is therefore named by a registered ancestor, so dynamic identifiers
cannot reach the log. Site labels are looked up the same way. The class
metadata is read through the descriptors of `type` itself, so no metaclass
hook or descriptor of the exception's class runs while naming it, and naming
never raises: a class whose metadata cannot be read is recorded under a
neutral name.
The same site and class are emitted
at most once a minute with a count of suppressed repeats, and the repeat state
holds a bounded number of keys.

The diagnostic path never raises into its caller. Its stream handler drops
its own write, flush or closed-stream failures silently instead of using the
standard logging error dump, which prints a traceback that would include the
exception being survived; the package logger does not propagate to handlers
it does not control. The path is also safe in a forked child: the module lock
is reinitialized after fork, and a descriptor-backed stream is written with one
`os.write` per record instead of through its Python buffer, whose lock another
parent thread may hold at fork time.

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
