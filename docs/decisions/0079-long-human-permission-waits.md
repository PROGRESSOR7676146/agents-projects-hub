# ADR 0079: long human permission waits

Status: accepted; source implementation, installed acceptance pending.
Date: 2026-10-27
Owner: repository owner
Contract: [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).

## Decision

A person may answer a protected permission hours later. A short card expiry or
an unchanged process deadline would make that impossible. Align the existing
journal, protected transport, tlive and synchronous native hook around a seven-day
human response window, with small outer hook margins. The finite values fit
native JavaScript timers. Keep connect, framing, shutdown and cleanup waits short.

Use one process-memory clock per existing permission host. Only a validated live
request prepared in the existing single-use journal starts that clock. The owned
process runner subtracts that waiting from its productive budget; stream messages
and provider claims cannot pause it. Stop imposes its independent cleanup cutoff.
Binding revalidation and single-use consumption still precede native Allow.

This uses the existing protocol and schema with no daemon, durable continuation,
automatic renewal, replay or permission on silence. Old tlive rejects an extended
expiry safely. Update the idle worker before the matching tlive, as in ADR 0077;
rollback reverses that order. Restart or channel loss denies pending requests.

## Evidence and limits

The Hub maintainer owns this component. ClaudePermissionJournal retains each
SQLite transaction; the permission host owns wait entry/exit and socket cleanup.
The external worker forwards the clock to the existing owned invocation boundary
in ExternalCliAdapter. The process runner retains process-group and pipe cleanup.
Timeout after invocation retains uncertain-outcome handling; neither waiting nor
its failure authorizes replay or another provider call.

Offline clock, subprocess, hook and Unix-socket fixtures exercise long waiting,
resumed productive limits, invalid-request refusal and bounded cancellation.
The carried tlive IPC suite checks signed decisions within the extended window
and rejects overlong/expired requests. These tests use fictional peers, not real
human callbacks. Installed and Telegram acceptance remain separate.
