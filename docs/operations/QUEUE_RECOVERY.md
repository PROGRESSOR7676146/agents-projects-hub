# Queue and process recovery

Status: active runbook  
Last updated: 2026-08-30

This runbook covers the durable Controller, provider-worker, and Telegram-outbox
topology. It contains reusable procedures only; deployment identities, paths,
chat IDs, account hints, and live evidence stay in private operator records.

## Safety rules

- Back up the SQLite database before migration or manual investigation.
- Repair or restart only the failed component. Controller, workers, sender,
  Hermes, and tlive deliberately have no mandatory service dependency chain.
- Do not edit queue tables by hand, copy a live SQLite file, or turn an
  `executing`/`indeterminate` job back into `queued` merely to make it move.
- Do not remove a Codex Unix socket until local process and lock inspection
  proves that no live owner can exist.
- Never paste configuration, tokens, provider output, or database rows into
  Telegram or a public issue.

## Read-only triage

```bash
agents-projects-hub doctor HUB_CONFIG
agents-projects-hub status HUB_CONFIG
agents-projects-hub monitor HUB_CONFIG
systemctl --user status agents-projects-hub.service
systemctl --user status agents-projects-hub-sender.service
systemctl --user status 'agents-projects-hub-worker@*.service'
```

Interpret the components independently:

- Controller down: new Telegram ingress and local commands stop; committed
  queue work and independent recovery channels remain.
- One worker down: only that provider stops taking new jobs; other workers and
  Controller commands remain available.
- Sender down: completed provider results remain `result_ready`; workers MUST
  NOT repeat provider execution to compensate for missing Telegram delivery.
- Hermes or tlive down: the other channels remain independent; no timeout is an
  approval.

## Component restart boundaries

After inspecting the failed unit and preserving its logs privately, restart
only that unit:

```bash
systemctl --user restart agents-projects-hub.service
systemctl --user restart agents-projects-hub-worker@AGENT.service
systemctl --user restart agents-projects-hub-sender.service
```

`SIGTERM` is cooperative. Polling and transport waits are bounded. A lease
observed after stop but before invocation is returned without consuming an
attempt. A signal can race after the final safe boundary; work past that point
uses the conservative rules below.

## Provider-job recovery

- Expired `leased` means provider invocation was not recorded as possible. The
  owning worker may safely return it to `queued` through normal stale recovery.
- Expired `executing` means invocation may have begun. Normal stale recovery
  marks it `indeterminate` unless a provider-specific structured reconciliation
  proves a result or proves that execution never began.
- `failed` and `cancelled` are terminal. Do not reinterpret them as pending.
- `result_ready` means provider work already succeeded. Only Telegram delivery
  remains; never submit another provider turn for the same job.

If a provider has no safe reconciliation capability, retain the
`indeterminate` record, inspect the project and provider session locally, and
create a new explicit user request only after deciding whether duplicate side
effects are acceptable.

## Changing provider ownership

Before changing a locally queued provider to `managed_externally`, stop new
admission for that provider and inspect its durable jobs. Drain `queued` and
`retry_wait` work with the old eligible worker, let the sender finish
`result_ready` work, and apply the conservative recovery rules above to any
`leased` or `executing` row. Cancellation is allowed only as an explicit local
operator decision for work that state validation still identifies as safely
unstarted; do not edit SQLite directly.

The Controller deliberately refuses startup when a configured externally
managed agent still owns any nonterminal local queue row. This is a diagnostic
barrier, not an automatic migration: restore the prior locally managed
configuration to drain safe work, or reconcile/cancel it through reviewed state
operations, then validate the new configuration again. Never start a native
gateway and a local worker as competing consumers for the same provider.

## Telegram outbox recovery

- An expired `sending` lease returns to `pending` through sender-scoped stale
  recovery. The provider result is not recomputed.
- Telegram may have accepted a message immediately before sender loss. A retry
  may therefore publish one bounded duplicate; this is safer than repeating a
  model turn or file-changing action.
- Retry exhaustion leaves the outbox and provider result terminally `failed`.
  Preserve both records for diagnosis. The current product has no remote or
  automatic force-resend command; recovery requires a reviewed future tool or
  a new explicit user publication, not direct SQL mutation.

## Managed Codex socket

The managed app-server holds an exclusive mode-`0600` sidecar lock containing
only PID and process-start metadata. Startup refuses to unlink an existing
socket it cannot prove it owns.

For a stale path, first verify locally that the recorded PID/start marker is
not a live matching process and that no process accepts the socket. Stop the
owning unit before any cleanup. If ownership remains uncertain, leave the path
in place and use the official stdio fallback or local recovery rather than
deleting it.

## Replacement hardware and writer leases

Completed SQLite state and provider session IDs can be restored with the local
configuration and project repositories. An in-flight turn is not portable.
Before resetting a `local` or legacy terminal writer lease on replacement
hardware, prove locally that the old host and its processes cannot still run.
The current schema does not store enough host identity to automate this proof,
so writer reset remains a reviewed local operation; Telegram cannot authorize
it.

## Queue rollback

1. Stop the selected external worker from taking new work.
2. Let safe work drain or classify remaining leases; do not cross an
   `executing` ambiguity boundary.
3. Keep the standalone sender running until prepared outbox rows are delivered
   or retained as explicit failures.
4. Change only the documented rollout gate. Do not remove queue tables or
   restore a migration backup for an ordinary runtime rollback.
5. Validate and run fault acceptance before resuming routine work.

Migration backup restoration is reserved for a failed migration. Runtime
rollback retains accepted jobs, results, outbox rows, and diagnostic history.

## Automated fault gate

Before a live queue cutover, run the full repository validation gate. Its
fictional subprocess matrix terminates child actors after Controller commit but
before offset persistence, during provider execution, and after fake Telegram
acceptance but before delivery persistence. It also covers pre-execution lease
recovery, concurrent provider isolation, and separate Hub/provider polling
offsets. This automated evidence does not replace the owner-driven Telegram and
provider acceptance required for a deployment.
