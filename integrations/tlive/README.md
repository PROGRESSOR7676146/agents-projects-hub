# tlive 5.3.1 protected permission extension

This package holds a source patch for the MIT licensed tlive 5.3.1 release. The
upstream copyright and license are in `LICENSE.upstream`. The pinned npm release
archive has SHA-256
`6d5a46ea36a243249b333f1034652a47a4c0b193485f407547e5870ad7b4aba1`.
`source-manifest.json` pins every non-generated release file and every changed
source file. No npm archive, dependency tree, or built binary is kept here.

For a caller-selected, unpacked **pristine** release directory:

```sh
python scripts/tlive_extension.py check-base /tmp/example-tlive/package
python scripts/tlive_extension.py apply /tmp/example-tlive/package
python scripts/tlive_extension.py verify /tmp/example-tlive/package
python scripts/tlive_extension.py build /tmp/example-tlive/package
```

`--archive /path/to/release.tgz` optionally verifies the original archive too.
`build` uses only dependencies already present in that selected source tree; it
never installs packages. Apply stages a full copy beside the source and uses an
atomic Linux directory exchange after both patch and final hashes pass. A
mismatch or unavailable atomic exchange leaves the selected tree unchanged.
The patch is idempotent only when all pinned final bytes already match.
An optional source-level smoke check runs without Telegram or IPC:

```sh
TLIVE_SOURCE=/tmp/example-tlive/package node --experimental-transform-types \
  --loader ./integrations/tlive/ts-source-loader.mjs \
  ./integrations/tlive/host-smoke.mjs
```

The Codex companion subscribes with `thread/resume` and `excludeTurns: true`.
This preserves live approval events without transferring the thread's full
history or overriding execution policy. A stalled history response is a
regression fixture, not proof of the cause of a particular installation's
disconnect. The supported field is documented in the
[Codex app-server reference](https://developers.openai.com/codex/app-server/).
Run its offline source regression with the selected tree's existing dependencies:

```sh
TLIVE_SOURCE=/tmp/example-tlive/package node --experimental-transform-types \
  --loader ./integrations/tlive/ts-source-loader.mjs \
  ./integrations/tlive/codex-subscription.test.mjs
```

It checks card delivery through the real companion and permission router,
waiting for an explicit decision, one-shot resolution, stale-answer rejection,
and disconnect cleanup. It neither starts a daemon nor proves installed
Telegram delivery; that still requires a real human approval roundtrip.

The separate `ipc-adversarial.mjs` suite uses the actual bootstrap, Unix IPC and
Telegram adapter with fake Telegram/app-server custody. Run it against verified
patched source using `ipc-test-loader.mjs`. It requires socket bind permission;
the Hub's real SQLite consumption is covered separately by its Python roundtrip
tests. See the [Hub runbook](../../docs/operations/CLAUDE_FILE_PERMISSIONS.md).

The host starts this channel only when its private `protected-permissions.json`
is explicitly present with version 1, an exact positive private chat ID and
owner user ID, plus distinct 32-byte lowercase hex request/result keys. The
file must be a single-link, owned regular file at mode 0600; the adapter must
name that sole chat and allow that owner. These values belong outside Git.
The protected namespace does not enter the ordinary permission router, web,
inbound text, or plugin continuation path. Hub must supply its own
PermissionRequest-only shim and must validate and consume signed receipts
against its live job, session, generation, root, lease, writer, and stop state.
The card shows the complete input for the bounded file tools and Bash or denies
without sending. The trusted Hub profile decides which tools are available;
legacy policy and Always-Allow grants cannot approve this channel. This package
does not enable Claude tools, alter an installation, or
establish native/Telegram/live security acceptance.

The explicit protected `Bash until restart` callback is separate from legacy
Always-Allow. A matching Hub worker authenticates and scopes its temporary
consent under [REQ-SEC-008](../../docs/product/ACCOUNTS_CONTROL_AND_SECURITY.md);
the daemon itself stores no reusable grant. Both sides must be updated for this
option. Settlement starts a single outcome edit immediately, retaining the
preview and removing buttons without waiting for native command completion.
Cosmetic failure cannot change the permission response. Source tests cover
failed/hung edits and a send receipt arriving after timeout.

The combined patch also scopes native approval cancellation to exact item/request
identity and makes companion subscriptions metadata-only (`excludeTurns: true`).
It includes the tlive subscription prerequisite from ADR 0073's source review;
no unrelated Claude runtime slice is included.
`build` now runs every carried offline regression and protected-host/IPC suite.
`test` runs those suites without rebuilding. Socket denial fails the gate.
The release archive omits upstream tsconfig/plugin-lock tooling; these checks
must not be described as full upstream CI or typecheck.

`TLIVE_CODEX_APP_SERVER_MODE=attach-only` disables server spawning at startup and
on every reconnect/probe failure. Stopping the client never stops the external
server. `auto` retains upstream custody for installations that have not opted in;
unknown mode values fail. Use the explicit
[migration and rollback procedure](../../docs/operations/TLIVE_RELIABILITY.md).
The passive `approval.snapshot` IPC exposes only bounded native identities,
delivery states and fresh daemon epoch/timestamps. It grants no approval or
replay authority and does not expose protected Claude requests.
