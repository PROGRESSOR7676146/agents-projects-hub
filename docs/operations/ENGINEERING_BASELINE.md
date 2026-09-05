# Engineering baseline backlog

Status: accepted findings; implementation evidence required  
Date: 2026-09-04

This backlog records reusable engineering defects discovered during the native
session-transfer readiness audit. It contains no deployment identities or live
transcripts. Items are ordered by the risk of continuing development without
them.

## P0: before the next live feature acceptance

1. **Restore a reproducible green gate.** Define one canonical offline-capable
   validation command using the repository environment. Running the script with
   an unrelated system Python currently fails before validation because the
   package is not importable. CI and local acceptance must execute the same
   formatter, lint, type, test, registry, and privacy stages.
2. **Protect the current work remotely.** Do not accumulate feature work on a
   branch that is many commits ahead of its remote without a reviewed push or
   equivalent remote backup. A local clean tree is not disaster recovery.
3. **Make the deployed revision observable.** Implement ADR 0012 phase one and
   refuse to call a deployment accepted while required processes report
   different or unknown revisions.
4. **Align running processes before E2E.** Long-running processes must be
   restarted through the controlled rollout only after queue/outbox inspection
   and SQLite-consistent backup. Service start timestamps alone are not revision
   evidence.
5. **Classify Telegram transport failures.** Replace the undifferentiated
   `TelegramError` health detail with bounded structured operation and failure
   class data: polling versus send, network versus API rejection, safe HTTP
   status/retry-after when present, consecutive failure count, and last success.
   Never record token-bearing URLs, payload text, or chat identifiers.

## P1: before calling the alpha operationally mature

1. **Immutable release activation.** Complete ADR 0012 phase two. Development
   checkout, active release, and rollback artifact must be distinct.
2. **Bound runtime-event storage (repository-complete).** Schema 21 and the
   runtime write path enforce deterministic 30-day/10,000-row retention while
   preserving current health, active alerts, and provider work. Migration,
   boundary, count, and rollback-on-fault tests cover the contract; no manual
   database editing is required.
3. **Make health semantics honest.** A component with repeated Telegram errors
   must not remain indefinitely indistinguishable from a fully healthy channel.
   Use bounded thresholds and edge-triggered recovery rather than alert spam.
4. **Repair recovery diagnostics (repository-complete).** User-service probes
   distinguish active, confirmed inactive, and unavailable supervisor state;
   Hermes heartbeat and tlive runtime liveness remain independent evidence.
   Separate tests prevent probe failures from being labeled inactive.
5. **Synchronize release metadata.** Package version, changelog, project status,
   Git tag, and deployed SHA have different purposes but must not contradict one
   another. Missing tags are visible release debt, not a reason to invent or
   rewrite history.
6. **Move live configuration out of the checkout.** Production configuration is
   private state under the operator configuration directory. A Git-ignored file
   inside `config/` is safer than a tracked secret but remains vulnerable to
   packaging, cleanup, and accidental publication mistakes.

## P2: maintainability and product evidence

1. **Split the product requirements by stable capability.** The current
   monolithic document is large enough that the privacy gate requires review on
   any edit. Replace it through a reviewed migration with a short normative
   index and stable requirement modules; preserve requirement IDs and links.
2. **Use an evidence vocabulary.** Every completion claim names its level:
   static check, unit test, adapter contract, synthetic fault test,
   deployment-local Telegram E2E, restart E2E, or cold-boot E2E. “All green”
   without the exact revision and evidence level is not an acceptance claim.
3. **Test behavior, not only prompt presence.** Telegram interaction contracts
   need provider-specific behavioural scenarios for short, ambiguous, long, and
   artifact-producing tasks. Prompt-string injection tests prove wiring, not
   user-visible style.
4. **Expose prompt-contract provenance diagnostically (repository-complete).**
   The acknowledged contract version is persisted per provider session and
   local `doctor` lists bounded current-session provenance. The ordinary mobile
   `/status` remains unchanged; live acceptance is tracked separately.
5. **Keep deployment evidence private and bounded.** Store exact live evidence
   outside Git, but record reusable acceptance procedures and failure classes in
   the repository.

## Definition of done for engineering changes

A change is complete only when its observable contract is documented, the
narrow tests and full canonical validation pass for a named clean commit, the
privacy gate passes, deployment uses that exact revision, rollback remains
available, and any required live/restart acceptance is explicitly distinguished
from repository-only evidence.
