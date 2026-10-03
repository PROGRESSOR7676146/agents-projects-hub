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
The card shows the complete input for the five bounded tools or denies without
sending. This package does not enable Claude tools, alter an installation, or
establish native/Telegram/live security acceptance.
