# Nightly Hub admission

Stage 1a reads one exact prepared plan and outputs a private, deterministic JSON
ticket. The installation keeps the plan and release artifacts under the
existing deployment retention rules. [ADR 0117](../decisions/0117-nightly-hub-admission-evidence.md)

```sh
python3.12 -I scripts/hub_deploy_ticket.py \
  --installation /home/example/private/installation.json \
  --plan /home/example/private/ops/plan-example \
  --plan-sha256 PLAN_SHA256
```

Pass the plan SHA-256 printed by `hub_deploy plan`. Read the same ticket again
with its exact `--ticket-sha256`. Readback performs local verification and
outputs JSON on success. It only inspects saved inputs and the loaded service
configuration. The candidate must include the native `release-manifest
backup-inputs` reader. Plans made before asset catalogs were bound need a fresh
explicit preparation. Candidate selection, release provenance, hosted gates and
the rollback rehearsal remain prerequisites owned by the release procedure.
[Deployment procedure](HUB_DEPLOY.md)

The output contains `ticket` and its `sha256`. All ticket and projection digests
use SHA-256 of canonical UTF-8 JSON, with sorted keys, no optional whitespace
and non-ASCII characters unescaped. `before_sha256` binds `before`. That record
contains the active manifest and its artifact and snapshot identities, primary
and worker config digests, registry, schema, database inventory and configured
Hub service commands. Commands come from systemd's structured `ExecStartEx`
readback and are represented by digests of their argv arrays. The profile
requires simple single commands in the managed release. The ticket also
carries the exact target and compatible rollback assets, sealed runtime pins,
expected post-switch backup inputs and the saved plan's managed file changes.
Configuration bytes, process arguments and environment values stay private to
their owners.

## Archived backup evidence

The archive adapter builds the same `before` projection from its verified
inputs and archived evidence. It writes `hub_baseline_sha256` into its format 1
archive manifest before sealing it. It derives that digest from the inputs it
actually archived and validated. The ticket supplies the expected value for
comparison.

The verifier resolves the exact run ID inside the configured private runs
directory. It reads that run's `SUCCESS.json`, checks the two snapshot IDs and
positive artifact coverage, and requires the digest of `bundle/manifest.json`
to match the receipt. It then compares the archived baseline digest with the
ticket and returns the success receipt's exact digest for later binding.
Missing retained evidence, legacy manifests lacking the baseline digest,
degraded coverage and malformed or mismatched evidence refuse. The external
runner owns verification of its archive and publication of terminal success.
The verifier uses local evidence under the trusted installation model.

## Executor acceptance

Stage 1b must reproject the exact ticket and validate its run receipt under the
deployment lock before effects. The maintenance-aware executor must durably
bind the run ID, ticket, plan and receipt to a single window before the first
effect. A repeat or lost response must read the original window. The deadline
must allow recovery after effects begin. A separate observer must report the
confirmed outcome and pin the verified in-window manifest and SQLite snapshot
for readback after convergence. Exercise interruptions, refusal on drift,
confirmed rollback with retained state, and lost output on the isolated stand
before installing a scheduler.
