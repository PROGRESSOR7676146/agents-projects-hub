# Live canary and rollback

Status: owner-coordinated acceptance procedure
Last updated: 2026-08-31

This procedure is the final gate between the automated subprocess fault matrix
and routine use of the external queue topology. It changes live Telegram and
local service state, so it MUST run only in a declared maintenance window with
the operator present. Real configuration, identities, transcripts, screenshots,
logs, and acceptance evidence stay outside Git.

## Stop conditions

Do not begin, or stop at the next safe boundary, when any of these is true:

- the full repository validation gate or required CI checks are not green for
  the exact deployed commit;
- the current SQLite state has any nonterminal provider job (`queued`,
  `retry_wait`, `leased`, `executing`, or `result_ready`) or any nonterminal
  outbox row (`pending` or `sending`), or has an unresolved `indeterminate`
  job; adopt and drain or classify every such row before cutover;
- a consistent state backup and the previous executable/configuration are not
   available;
- the shadow Hub configuration does not validate or does not select every
  locally managed productive provider as an external worker;
- the standalone sender, one worker per local provider, the Controller, and the
  required provider direct-message services cannot be supervised independently;
- the recovery channel expected to remain available during the canary is not
  healthy.

Never force an ambiguous provider job back to `queued`, edit queue tables, or
delete a socket merely to make the canary continue.

## Prepare without cutover

1. Record the exact commit and package version privately. Verify CI and CodeQL
   for that commit, then run `python scripts/validate.py` from the deployed tree.
2. Create an SQLite-consistent backup with `agents-projects-hub backup STATE
   [DESTINATION]`. Verify that the artifact can be opened. Preserve a rollback
   binary/configuration pair that is proven compatible with the current schema;
   test it only with an offline state-open or status operation against a
   disposable configuration whose `state_path` names a disposable copy of the
   backed-up database, never the live file. Do not launch `controller`, `serve`,
   `worker`, or `sender` for this compatibility check. If an older binary cannot
   read the schema, rollback must use the current binary with the previous
   runtime gates.
3. Create a mode-`0600` shadow configuration outside Git. Do not replace the
   active configuration yet. It must set:

   - a private `hub_bot.token_file`;
   - `dispatch_mode: "queue"`;
   - `queue_runtime: "external"`;
   - `outbox_runtime: "external"`;
   - every locally managed Codex, OpenCode, and Antigravity identity in
     `external_worker_agent_ids`.

4. The shadow configuration must intentionally reference the active, backed-up
   state database so offsets and queue continuity are preserved. Run
   `agents-projects-hub validate-hub SHADOW_CONFIG`; this is a comprehensive
   offline configuration check and therefore validates every configured local
   token file. It does not prove the Controller's role-scoped credential
   boundary; the Controller runtime loader reads only its selected ingress
   token. Run `agents-projects-hub status SHADOW_CONFIG` only after the backup
   and intended schema migration. Status makes no provider/network request, but
   opening state may initialize or migrate SQLite. Missing processes may be
   `unknown` before launch.
5. Confirm the service topology before changing Telegram settings: one central
   Controller, one standalone sender, one worker per local provider, and only
   the desired provider direct-message ingress units. No provider group ingress
   process may compete with the Hub Controller.
6. Confirm the intended Telegram policy privately. The Hub must receive the
   project-group updates needed for deterministic routing. Provider bot privacy
   and membership must prevent provider group admission while preserving their
   response identity and optional private-chat endpoint. Menu synchronization
   is a separate explicit external change.

Preparation ends here when restarts or Telegram changes are not authorized.

## Controlled cutover

Use the supervisor commands appropriate to the local installation; do not paste
real unit names, paths, or output into the repository. First perform an explicit
poller ownership handoff:

1. Stop the legacy provider/group ingress poller. Wait beyond its bounded poll
   return, then verify through the supervisor and process inspection that it is
   down and cannot call `getUpdates`. Do not start any replacement poller before
   this confirmation.
2. Confirm again that the durable queue/outbox has no nonterminal or ambiguous
   work. A message accepted after this check is a reason to stop and reconcile
   the old poller rather than continue.

Then start or switch only one layer at a time and inspect cached health after
each layer:

1. Start all selected external provider workers. They must become healthy
   independently and must not read Telegram credentials.
2. Start the standalone sender. It may read only the configured provider
   response tokens and must not own a provider adapter or model session.
3. Start the provider private-message ingress units that are part of the
   deployment. Verify that they use their own offsets, admit private messages
   only, and do not overwrite the Controller health identity. No process using
   the same provider token may still be polling.
4. Apply the intended Hub Telegram policy and start the central Controller with
   the shadow configuration. It must read only the Hub token, publish the `hub`
   ingress offset, and be the only process admitting project-group updates.
5. Keep the schema-compatible rollback executable/configuration available, but
   do not run any second project-group admission path, even under a different bot
   identity.

Any unexpected credential read, provider process under the Controller, shared
offset, repeated provider invocation, or cross-provider outage is an immediate
rollback signal.

## Canary sequence

Use one explicitly selected canary topic. Do not begin with a file-changing or
otherwise irreversible provider request.

1. Run `/status` and `/accounts`. They must remain compact and responsive
   without a productive model turn.
2. Send one harmless request separately to each locally queued provider that
   has a selected external worker. Verify one durable job, one provider
   invocation, one result, and delivery through the matching provider response
   identity. Test an externally managed provider such as the recovery agent
   separately through its native Gateway: Hub must not claim or enqueue that
   productive request, and no local worker may consume it.
3. Verify ordinary active-provider routing, explicit mention routing, and a
   real Telegram Reply to a provider response. A quoted message must remain
   commentary for the active provider rather than change its addressee.
4. Verify that idle providers produce no invocation or token use during another
   provider's turn and that the Controller receives the visible result through
   durable state rather than passive model observation.
5. With no productive job in flight, stop one provider worker as a deliberate
   fault. `/status` and another provider must remain usable. Restore only the
   stopped worker and verify its cached health transition.
6. With a prepared harmless result, exercise a sender interruption only if the
   operator accepts the possibility of a duplicate Telegram publication.
   Confirm that delivery retries without another provider invocation.
7. Verify the independent recovery channel while the Hub path is healthy; do
   not disable both control planes in the same test.

Store exact observations and logs privately. Repository documentation may state
only whether the reusable acceptance class passed.

## Acceptance and observation

The canary passes only when every applicable functional acceptance criterion in
the product requirements has evidence, cached health is stable, there are no
unresolved queue rows, and each service can fail without taking down an
unrelated provider or recovery channel.

Passing one turn is not authorization to remove the compatibility path. Keep
the previous executable/configuration through the documented rollback window
and complete routine observation before considering legacy-path retirement.

## Rollback

1. Stop new Hub admission at a bounded polling return.
2. Inspect queue state. Drain safe `queued`/`retry_wait` work through its current
   external owner before changing runtime gates. Preserve `executing` ambiguity
   as `indeterminate` when provider-specific proof is unavailable. If safe work
   cannot drain, keep the compatible external workers/sender and do not restore
   inline admission yet; an advanced Telegram offset with no queue consumer
   would strand accepted work.
3. Keep the standalone sender long enough to deliver or explicitly retain
   already prepared results. Do not rerun a provider to compensate for a
   Telegram failure.
4. Stop the external workers and sender only after all accepted work is terminal
   and their leases are safe or classified. Restore only a schema-compatible
   configuration/executable pair without deleting the additive queue schema or
   accepted records. A prior binary must never open an unknown schema version.
5. Restore the prior Telegram policy and legacy group ingress only after the Hub
   poller is stopped. Never run competing pollers for one identity.
6. Validate the restored path and record the failure class privately. Restore a
   database backup only for a failed migration, not for ordinary runtime
   rollback.

Use [QUEUE_RECOVERY.md](QUEUE_RECOVERY.md) for component-specific lease,
outbox, socket, and replacement-host recovery rules.
