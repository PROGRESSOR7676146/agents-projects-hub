# Delivery control preview prerequisite

Status: schema46 source prerequisite under validation. Full-control apply and
runtime exceptions are unavailable. Source owner: Hub maintainer.
The [owning requirement](../product/PERSISTENCE_AND_RECOVERY.md) and
[ADR 0063](../decisions/0063-delivery-control-reconciliation.md) define eligibility,
evidence and staged authority; this guide owns only the local procedure.

Use an existing current-schema state copy, with operator authority and its custody
boundary verified separately. The preview does not migrate, load deployment
configuration or credentials, contact Telegram or invoke a provider:

```bash
agents-projects-hub delivery-control /home/example/private/state.db final_outbox example-outbox
agents-projects-hub delivery-control /home/example/private/state.db progress_delivery example-progress
```

Output includes bounded identities/counts, the fresh snapshot, any historical
disposition snapshot and a diagnostic binding match. It always says
`capability=preview_only`, `apply_available=false`, `control_effect=not_enabled`.
Content and paths are not printed. Missing or older state fails rather than being
created/upgraded. There is no `--apply`, release or resend flag. A snapshot is a
stale-state fingerprint, not a secret, authentication or permission.

Schema44 `delivery-hold` remains the separate existing queue-only control, with
its disclosed lifetime/session/writer restrictions. Neither preview output nor a
storage fixture extends that consent. Do not edit a ledger row or delivery status
to simulate full-control recovery.

## Acceptance boundary

Offline migration and fault tests use fictional state. Native/Telegram acceptance
must be separately authorized after the public apply and coordinated consumers
exist. Cover parked unknown final/progress and exhausted failed delivery, original
parts/receipts/artifacts, repeated exact consent, another target in the same job,
later legitimate session/scope changes and independent uncertainty/writer/hold/stop
guards. Failed uncertain notices require existing exact terminal evidence or an
owner resolution before target retention; an unresolved replaceable failed notice
must still refuse. No blind resend, provider replay, fabricated receipt or restart
alone counts as reconciliation. Raw historical delivery counts remain visible.

Before any authorized migration to schema46, inspect an exact compatible rollback
artifact: schema45 executables cannot open the new state. A backup is not permission
to overwrite subsequent accepted work. Store deployment evidence outside Git and
name the exact clean revision and highest proven level.
