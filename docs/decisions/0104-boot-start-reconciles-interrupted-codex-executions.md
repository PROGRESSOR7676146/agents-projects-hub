# ADR 0104: The first boot start reconciles interrupted Codex executions

Status: accepted, 2026-11-16 (owner decision). Source implementation is separate
from installed acceptance.
Amends: [ADR 0088](0088-boot-time-codex-server-start.md) and REQ-OPS-005.

## Context

A host crash during a Codex turn leaves its provider job `executing`. At the
next boot the receipt-less first start attempt of the pinned app-server
([ADR 0088](0088-boot-time-codex-server-start.md)) refuses on that job and
spends the boot's only allowance. Workers then start without the server, and
their ordinary stale recovery settles the job within about a minute. The server
still stays down until another reboot or a manual receipt start. A manual
receipt start asserts that native clients are closed, and an agent running
inside Codex cannot assert that truthfully. This deadlock happened twice:
one outage lasted about 21 hours, the other healed by the next reboot.
In both cases the blocker was an interrupted execution, not queued work.

## Decision

1. The deploy tool records the installation's Hub configuration path as
   `hub_config` in the private server manifest, which it already rewrites for
   every release. Only the receipt-less first start attempt of a boot uses it.
   Receipt starts and manifests without the field are unchanged. The protected
   server unit template and its drop-in do not change.
2. That attempt first does every existing ADR 0088 check: it claims the boot
   marker, checks lingering, refuses native clients and existing servers,
   verifies the artifact pins and removes the stale link. Then it runs the
   ordinary worker reconciliation of interrupted executions:
   - The state database named by the Hub configuration must be the same file
     as the manifest's, or nothing runs.
   - The worker's model-free recovery claims only jobs whose lease has expired,
     under a new lease token. It reads the exact recorded thread and turn
     through the already hashed pinned binary over stdio.
   - The outcome is the worker's ordinary one: it publishes a confirmed saved
     result, or marks the job indeterminate with its failure notice.
   - It never replays, resumes or retries a turn and never touches queued work.
3. Before any temporary client or state change, the attempt refuses an existing
   socket or path at the manifest socket path, as the later gate would.
4. The reconciliation is bounded by one monotonic deadline of 150 s, cleanup
   included. Within it, the attempt waits for leases left by the crashed boot
   to expire. It claims a job only while a full handshake and exact read still
   fit, so the deadline never turns a confirmable result into an indeterminate
   one. Every client call of a claimed job ends 10 s before the deadline, which
   leaves time to terminate and reap the temporary client; the handshake also
   has its own 20 s bound. A lease that stays valid, for example after a wall
   clock jump, or that expires too late, stays in place. A failure is logged
   and changes nothing else.
5. Afterwards the attempt repeats the native-client and existing-server
   refusal, then runs the unchanged idle gate. Anything still blocking refuses
   the start exactly as before, and the boot allowance stays spent.

The previous rule was that fresh server creation never resolves, confirms or
replays execution uncertainty. That rule no longer holds for this one attempt:
it reconciles, and may confirm, executions interrupted by the previous boot,
using the worker's own procedure. Replay and resume stay forbidden, and so does
any change to historical delivery uncertainty.

## Consequences

- A crash during a Codex turn no longer leaves the server down after the
  reboot. Queued work at crash time still blocks the start;
  [ADR 0105](0105-boot-start-leaves-queued-codex-work.md) lets it stay queued
  when no Codex consumer is observed.
- The process scans remain snapshots, not an admission fence. Workers are
  ordered `After=` the server unit and wait for its start job. A client started
  between the scans is the existing ADR 0088 limitation, not a new guarantee.
- An expired lease and a claim token keep a resumed stale worker from writing
  a terminal job state. They do not prove the old owner is gone. At boot the
  kernel restart supplies that proof for every process of the previous boot.
- The start attempt can spend up to 150 s before the server starts, which
  leaves 120 s of the 270 s readiness budget and the 300 s start timeout. If the main process is killed, the temporary stdio client loses
  its input pipe and exits.
- The first deployment of a release with this change adds the field. An older
  runtime ignores it, and a deploy rollback restores the previous manifest
  bytes.

Live boot acceptance is pending: crash a fictional turn, reboot, and verify that
the server starts and the job is settled without replay.
