# ADR 0077: explicit Claude Bash consent until restart

Status: accepted; source implementation, live acceptance pending.
Date: 2026-10-27
Owner: repository owner
Contract: [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).
Amends the per-invocation-only Bash rule in
[ADR 0075](0075-claude-personal-developer-profile.md).

## Context and decision

A useful edit/test/Git cycle can need dozens of Bash invocations. Requiring an
individual click for each command makes ordinary development impractical.
Keep exact Allow once and Deny, and add a distinct human-selected `Bash until
restart` action. Its scope is one Claude native session/generation and canonical
project root on the accepting worker. The existing protected transport, native
hook, namespace and permission journal remain in use.

Store consent only in bounded worker memory. Authenticate the current tlive
epoch before reuse; worker restart erases memory and tlive restart invalidates
the epoch. No database migration, disk-backed policy or native settings grant
is needed. Extra worker slots can require another approval; this conservative
limit avoids shared grant infrastructure. The owner can revoke by restarting
the relevant worker or tlive. `/stop` stops work; it is not grant revocation.

The protected path also initiates a single outcome edit as soon as its callback
settles. It removes buttons, retains the original preview and identifies the
decision. Neither model completion nor cosmetic Telegram success gates the
permission response. Timeout/disconnect cleanup also handles a late send receipt.

## Evidence and consequences

The source fixtures cover signed callbacks, grant reuse across queued turns,
restart epochs, isolation/refusal, current binding checks, immediate settlement,
late delivery and failed/hung cosmetic edits. They invoke no remote model or
real Telegram API. Publication and live acceptance remain distinct.

This explicitly increases human-authorized Bash scope to commands not individually
previewed, including their descendants within the existing namespace. It grants
no additional filesystem or provider capability and claims no hostile-project
or same-UID isolation. Existing first-valid-answer, stop and no-replay boundaries
remain intact. Network latency can still delay the Telegram display.
