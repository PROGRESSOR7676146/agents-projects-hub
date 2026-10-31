# Telegram delivery and receipt requirements

Part of the [product requirements baseline](PRODUCT_REQUIREMENTS.md).
Supporting detail: [reference](REQUIREMENT_DETAILS.md#delivery-and-receipts).

## 23. Telegram result and progress delivery

- **REQ-QUEUE-005 (Implemented for embedded compatibility and the external sender):** Saved results
  MUST survive independently of Telegram delivery; retrying delivery MUST NOT repeat provider work
  or imply execution success.
  - A durable outbox MUST preserve ordered text/artifacts and acknowledge context only after
    successful result commit; external delivery ownership MUST remain separate from
    Controller/provider execution.
  - External delivery MUST fairly serve all local queue agents with their own identities, including
    mixed rollout, without provider adapters or RPC access.
  - Retry deadlines MUST survive restart, respect Telegram cooldowns and bounded backoff, and
    consume no attempts while waiting.
  - Stop recording and its single Hub acknowledgement MUST commit together; acknowledgement MUST NOT
    release uncertain execution, while no-work stops may reply directly.
  - Progress MAY deliver completed visible commentary separately, bounded and rate-limited with
    silent notification requested; it MUST NOT complete work, acknowledge context or authorize
    replay.
  - Final results MUST take priority, terminal work supersedes pending progress, and final/owner
    decisions retain normal notifications; silent flags do not prove absence of client alerts.
  - Every send MUST first persist its exact attempt, sender, destination and next unreceipted
    part/payload/artifact snapshot; only a real positive Telegram receipt proves acceptance.
  - Retry MUST require proven rejection or verified failure before any HTTP send, including
    proxy/redirect paths; labels alone are insufficient.
  - Ambiguous or expired attempted sends MUST remain unknown, preserving saved results, delivered
    prefixes and artifacts; unattempted expiry MAY requeue and receipt cleanup MUST NOT resend.
  - Legacy delivery MUST remain conservatively unknown/unverified; historical IDs MUST NOT
    manufacture validated whole-result provenance.
  - Exact late receipts MUST remain recordable after lease expiry; persistence retries and restart
    projection MUST NOT send again, and conflicts MUST refuse.
  - Receipt evidence MUST survive separately from mutable bookkeeping; newer attempts, changed
    bindings or owner consent MUST NOT allow mutation of a pinned delivery snapshot.
  - Reconciliation MUST wait for an active notice send to settle; unknown notices MUST retain their
    parts/history even when later terminal proof and bounded completion are saved.
  - Replacing a certain notice MUST atomically archive all parts, receipts and artifact references;
    missing historical parts or unreferenced new artifacts MUST NOT be invented.
  - Unknown final/legacy delivery MUST hold later topic outboxes and saved-result FIFO; restart and
    age MUST NOT release it.
  - Only an explicitly classified terminal resultless execution notice MAY avoid blocking later
    saved delivery; it grants no session/writer authority and leaves native/root/stop/hold checks
    intact.
  - Local-owner queue-only consent MUST use a read-only exact preview and explicit agreement, then
    atomically recheck and append one immutable decision without migration, credentials or network.
  - Consent MUST bind the job, canonical scope, project, numeric destination, result and all
    parts/receipt/artifact evidence; stale/conflicting apply MUST refuse and exact repeats report
    the original decision and current effect.
  - Preview MUST explain retained session-control, scope-transfer and drain restrictions; local OS
    authority MUST NOT be described as Telegram-authenticated identity.
  - Queue-only consent MAY release only that exact delivery/FIFO barrier; it MUST NOT change unknown
    evidence, saved work, permissions, stop/holds, writers, native uncertainty or other control
    guards.
  - Project/destination bindings MUST remain fixed; scope movement requires separate exact
    full-control consent for every retained queue-only hold.
  - Full-control consent MUST separately preview one parked unknown or policy-exhausted target
    without a sender lease, retain exact historical identity/evidence and require explicit
    acceptance of unconfirmed delivery.
  - Its preview MUST be read-only and content/path-free; apply MUST recheck and append only
    immutable consent, never replace another snapshot or reinterpret queue-only permission.
  - Failed indeterminate notices MUST already have exact terminal proof or owner resolution so
    recovery cannot replace the pinned target; prerequisite references MUST survive.
  - Full final consent MAY reconcile delivery waits in session/model/agent, transfer,
    connect/adoption, lane, relocation and drain controls; progress consent MUST NOT release a
    final-result wait.
  - Legitimate later changes MUST preserve historical consent, but mismatched
    project/destination/job/target/generation MUST make its effect fail closed; each target needs
    separate consent.
  - Neither consent grants resend, provider replay, Reply, assessment or approval authority, nor
    overrides native uncertainty, origin/root checks, owner holds, stop, writers, workflows or
    capacity.
  - Status, retry, outcomes, alerts and drain MUST distinguish raw unknown history from currently
    effective blockers.
  - Activation MUST include all affected consumers, exact-candidate independent review, compatible
    rollback and separate owner deployment authorization; incompatible pre-existing consent ledgers
    MUST refuse migration without loss.
  - With Hub and external queue/outbox enabled, unknown saved-result delivery blocking FIFO MUST
    create one independent durable source-topic Hub notice, even without a successor.
  - Discovery/send MUST recheck exact bindings and effective blockage, suppress stale unattempted
    notices, preserve attempted/unknown evidence and avoid starving unrelated delivery.
  - The notice MUST explain investigation and exact local consent restrictions, remain truthful as a
    saved observation, and grant no mutation, replay, resend, delivery confirmation or control
    authority.
