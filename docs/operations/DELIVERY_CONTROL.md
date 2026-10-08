# Delivery control reconciliation

Status: schema47 source implementation under validation; live activation pending.
Source owner: Hub maintainer.
The [owning requirement](../product/PERSISTENCE_AND_RECOVERY.md) and
[ADR 0063](../decisions/0063-delivery-control-reconciliation.md) define eligibility,
evidence and staged authority; this guide owns only the local procedure.

Use an existing current-schema state copy, with operator authority and its custody
boundary verified separately. Preview/apply do not migrate, load deployment
configuration or credentials, contact Telegram or invoke a provider:

```bash
agents-projects-hub delivery-control /home/example/private/state.db final_outbox example-outbox
agents-projects-hub delivery-control /home/example/private/state.db progress_delivery example-progress
```

Output includes bounded identities/counts, the fresh snapshot, any historical
disposition snapshot, historical binding match and current control effect.
The default invocation remains read-only; `apply_available=true` reports local
capability, not an already granted permission.
Content and paths are not printed. Missing or older state fails rather than being
created/upgraded. A snapshot is a
stale-state fingerprint, not a secret, authentication or permission.

After inspecting the exact target and independently confirming that its unknown
delivery may be accepted, copy that preview's snapshot into explicit local apply:

```bash
agents-projects-hub delivery-control /home/example/private/state.db final_outbox example-outbox --apply --snapshot SNAPSHOT_FROM_PREVIEW --accept-unconfirmed-delivery
agents-projects-hub delivery-control /home/example/private/state.db progress_delivery example-progress --apply --snapshot SNAPSHOT_FROM_PREVIEW --accept-unconfirmed-delivery
```

Each target needs its own fresh preview and consent. A stale first apply refuses;
an exact repeat returns the original immutable decision and current effect. A
different token cannot replace a prior decision. No resend or provider invocation
occurs. Delivery remains unknown/failed, and saved evidence is unchanged.

Schema44 `delivery-hold` remains the separate queue-only control. Its preview and
retry independently show a full-control effect if one exists; they never grant
that authority themselves. Do not edit a ledger row or delivery status to simulate
recovery. Remaining native/writer/owner/stop/workflow/root/capacity checks apply
even when `control_effect=delivery_wait_reconciled`.

## Acceptance boundary

Offline migration and fault tests use fictional state. Native/Telegram acceptance
must be separately authorized on the exact coordinated candidate. Cover parked
unknown final/progress and real delivery-policy-exhausted failed delivery, original
parts/receipts/artifacts, repeated exact consent, another target in the same job,
later legitimate session/scope changes and independent uncertainty/writer/hold/stop
guards. Include two schema44 holds in one topic: one full consent must not unlock
the other's scope freeze. Verify final/progress targets independently, default
read-only CLI, coherent concurrent status, stale CAS, rollback and historical
consent after ordinary session/model/provider/scope changes. Project/destination
and generation drift must invalidate effect. Failed uncertain notices require existing exact terminal evidence or an
owner resolution before target retention; an unresolved replaceable failed notice
must still refuse. No blind resend, provider replay, fabricated receipt or restart
alone counts as reconciliation. Raw historical delivery counts remain visible.

Before any authorized migration to schema47, inspect an exact compatible rollback
artifact: schema46 executables cannot open the new state. A backup is not permission
to overwrite subsequent accepted work. Store deployment evidence outside Git and
name the exact clean revision and highest proven level.
