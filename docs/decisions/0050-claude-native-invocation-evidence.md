# ADR 0050: Claude native invocation evidence

Status: accepted; implementation in progress
Date: 2026-10-02

## Context

The initial [Claude scaffold](0039-claude-cpa-worker-scaffold.md) stores its
session only after successful execution and reads the process output after it
exits. A crash can therefore lose the new native identity, and a large stream
can consume unbounded memory before validation. Generic CLI failures also do
not distinguish a verified terminal rejection from an unknown outcome.

The official CLI supports a caller-supplied session UUID and exact resume.
Structured assistant messages have native message UUIDs, but the inspected
interface does not supply the Codex-style accepted turn identity used by the
existing execution journal. A session or message UUID must not be relabelled
as a native turn ID.

## Decision

Before invocation, the journal atomically checks the execution lease and the
Hub session's topic, agent, generation and writer, then persists the native UUID
and canonical root. A job admitted before the first turn may have no UUID in
its immutable snapshot; it resumes the current UUID of the same generation.
An explicit snapshot must match. Resume requires earlier matching root evidence
before writing the new checkpoint. Missing or conflicting provenance fails
visibly before invocation; it never silently creates a replacement conversation.
Older Claude sessions without that evidence require explicit reset or a future
validated adoption path. Repeating preparation for the same job is not a second
invocation authority. A UUID allocated before a proven route, credential or
process-launch rejection remains the same UUID on the next new start. Reusing
new-start mode requires every earlier bound attempt to prove non-invocation;
missing history, native evidence or uncertainty cannot authorize recreation.

The Claude process reader drains stdout and stderr concurrently with bounded
memory, event count and visible text. It validates the complete structured
stream, requiring one consistent terminal outcome and exact session identity.
A verified terminal failure is a failed job; malformed, missing, contradictory
or mismatched output remains indeterminate. Structured quota evidence may mark
the provider limited; percentages and reset time remain unknown unless reported.
Neither outcome automatically replays work. A covering stop keeps the precedence
and certainty boundaries of [ADR 0049](0049-task-visibility-and-stop-certainty.md).

Only completed main-assistant visible text may cross the provisional callback.
Native message identity deduplicates it; hidden reasoning, tool payloads,
subagent messages and raw diagnostics are excluded. Provisional text is not a
successful result or terminality proof. Durable journal/recovery wiring must
bind it to the prepared invocation without inventing a native turn ID.

## Ownership and limits

The journal owns the preparation transaction, the worker owns the one provider
invocation, the adapter owns stream validation and process cleanup, and the
existing sender owns Telegram delivery. A caught local failure after validated
completion persistence uses the same recovery path; if local delivery preparation
fails again, the saved result and root remain held until bounded lease recovery. The immutable job snapshot is preserved.
Extracting the Claude parser and process reader resolves the previous run-turn
hotspot without changing other provider process paths.

This decision does not enable tools, hooks, approvals, a write-capable lead or
an advisor role. The [security contract](../product/ACCOUNTS_CONTROL_AND_SECURITY.md)
still requires an accepted human host and technical role isolation. Local
transfer, saved-session discovery, model metadata and subscription route
acceptance remain separate parity work. A loopback CPA check is not evidence
of its upstream account or billing route.

## Evidence required

Tests must observe the committed UUID through a second connection inside the
fake provider invocation; cover queued-before-first-completion continuity,
wrong root/generation/writer, immutable snapshots, stale leases and atomic
rollback; and distinguish exact terminal rejection from conflicting output.
Process tests must cover output floods before EOF, timeout, interruption,
descendant cleanup, chunked UTF-8 and callback persistence failure. Recovery
must retain visible partials and completed evidence without a productive replay.
Canonical validation, independent exact-revision review and separately
authorized native/Telegram acceptance remain distinct gates.

Official interfaces: [CLI reference](https://code.claude.com/docs/en/cli-reference),
[programmatic use](https://code.claude.com/docs/en/headless), and the public
[assistant-message type](https://code.claude.com/docs/en/agent-sdk/typescript#sdkassistantmessage).
