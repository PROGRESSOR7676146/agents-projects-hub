# Stack update tool

Status: stage 1 of [ADR 0048](../decisions/0048-out-of-band-update-and-incident-plane.md),
implemented and tested offline; no deployment uses it yet.
Contract: [REQ-OPS-015](../product/PERSISTENCE_AND_RECOVERY.md).

`stack-update` updates the external components that the Hub depends on, such as
the provider CLIs, tlive, and a local provider proxy. It does not update Project
Hub itself: Hub releases keep their own
[immutable procedure](IMMUTABLE_RELEASES.md). This runbook covers operation.
The ADR owns the rationale; the requirement owns the contract.

## What it guarantees

- **Plans install and run nothing.** `plan` reads version metadata from each
  component's configured source and writes a plan. For an npm component it also
  resolves the complete dependency tree with `npm install --package-lock-only
  --ignore-scripts`, which downloads no package and runs no package code. The
  plan records:
  - the exact versions and sources;
  - the bytes it binds:
    - for npm, the full lockfile, so every dependency's version and integrity
      is pinned, and the registry `integrity` of the package;
    - for an archive, the SHA-256 from the configured checksums file (GNU or
      BSD format). When a source publishes no checksums, `plan` downloads the
      archive and records its SHA-256;
  - every current link;
  - the manifest digest.

  It is sealed by its own digest.
- **Apply works on one exact plan.** `apply` needs the plan digest the owner
  approved. It refuses if the plan no longer matches its digest, or if the
  manifest file or any link differs from what the plan saw. It checks this
  again right after staging, which can take minutes, before the first link
  changes. A successful switch re-pins the manifest, so a plan cannot be
  applied twice.
- **Nothing changes before the gates pass.** `apply` does these steps in order:
  1. Stages each candidate into an immutable version directory. An archive is
     compared with the digest in the plan. An npm component is installed with
     `npm ci --ignore-scripts` from the planned lockfile, and the installed
     tree is compared with it. Only then, and only when the manifest allows
     them, `npm rebuild` runs the packages' lifecycle scripts.
  2. Runs the staged gates: the version output, then the `staged` checks.
  3. Only then touches links. A staging or check failure leaves every link
     unchanged.
- **Switches go in dependency order, with automatic restoration.** Components
  switch in ascending `order`, one at a time. For each one, `apply` records the
  intent durably, flips the link atomically, restarts the component's units,
  and waits for them to be active and for its `live` checks to pass. A failure
  at any point up to the final record write (a failed gate, an unexpected
  error, a failed pin or record write, or an interrupt) puts back every switched
  link in reverse order, restarts those units, and restores the previous pins.
  The record ends as `restored`, or `restore_failed` if restoration failed too.
- **Durable records.** Records, pins and link flips are written with `fsync`
  of the file and its directory, so after a host crash a switched link always
  has its recorded intent.
- **One operation at a time.** `apply` and `rollback` take one exclusive lock.
  A concurrent request is refused, not queued.
- **Rollback works from the record.** Each switch record keeps, per component,
  the link path, the targets before and after, the units, and the previous pin.
  `rollback SWITCH`:
  - undoes exactly those links;
  - refuses before changing anything if a link now points elsewhere, or if the
    manifest names another link path for the component;
  - is idempotent: it restores links, restarts units and restores pins again,
    so a rollback interrupted halfway completes when repeated;
  - marks the switch as rolled back once it completes, so it cannot be applied
    a second time. A switch that `apply` already restored is refused.
- **Guarded downloads.** Every redirect is checked before it is followed:
  HTTPS sources may not be redirected to plain HTTP, and loopback checks may
  not be redirected off the machine.
- **No live inference unless asked.** A check marked `"inference": true` runs
  only with `--allow-live-inference`, which is an explicit owner request
  (maintenance rule 8).

The model-free approval controls and the Hermes integration arrive in stage 3.
Until then, run the tool only by hand.

## Manifest

The manifest is private deployment configuration and never enters Git. Its
default location is `${XDG_CONFIG_HOME:-~/.config}/agents-projects-hub/stack-manifest.json`.
It drives command execution, so the tool refuses a symbolic link, a file that
is not owned by the current user, or one that is group- or world-writable. The paths below are
fictional:

```json
{
  "schema_version": 1,
  "state_dir": "/home/example/.local/state/agents-projects-hub/stack-update",
  "npm": "/home/example/.local/opt/node/bin/npm",
  "components": [
    {
      "id": "proxy",
      "order": 10,
      "link": "/home/example/.local/opt/proxy/current",
      "versions_dir": "/home/example/.local/opt/proxy/releases",
      "version": "1.0.0",
      "source": {
        "kind": "archive",
        "url": "https://example.com/proxy/v{version}/proxy_{version}_linux_amd64.tar.gz",
        "checksums_url": "https://example.com/proxy/v{version}/checksums.txt",
        "github_repo": "example/proxy"
      },
      "version_argv": ["{dir}/proxy", "--version"],
      "units": ["proxy.service"],
      "checks": [
        {"phase": "live", "url": "http://127.0.0.1:8317/v1/models", "expect": "data"}
      ]
    },
    {
      "id": "codex",
      "order": 20,
      "link": "/home/example/.local/opt/codex/current",
      "versions_dir": "/home/example/.local/opt/codex/releases",
      "version": "0.1.0",
      "source": {"kind": "npm", "package": "@example/codex"},
      "version_argv": ["{dir}/node_modules/.bin/codex", "--version"],
      "units": ["tlive.service", "agents-projects-hub-worker@codex.service"]
    }
  ]
}
```

Manifest rules:

- **Placeholders.** `{version}` is the only placeholder in source URLs, and
  `{dir}` the only one in argv. `version_argv` and staged checks run against
  the staged version directory; live checks run against the link.
- **URLs.** A `url` check must use loopback HTTP(S). Source URLs must be HTTPS.
- **Units.** `units` lists every user unit that must restart to pick up the new
  version, including consumers such as a Hub worker that runs the CLI.
- **Hermes.** A component that needs the Hermes self-update watchdog
  (`"watchdog": true`) is refused until stage 4.

## Moving a component into version directories

`plan` refuses a component whose link exists but does not point into its
`versions_dir`. A component installed in place by a package manager moves once,
by hand, with owner authorization:

1. Install the running version into `versions_dir/VERSION`.
2. Add a marker file with
   `{"component": ID, "version": VERSION, "artifact": {}}` as `.stack-update.json`.
3. Point `link` at that directory.
4. Change the executables or units to run through `link`.
5. Restart the affected units.

## Commands

Install a pinned copy first, so the tool keeps working when a Hub release or
its virtual environment is broken:

```bash
python3 -m hermes_codex_router.stack_update install-copy
```

It copies the single standard-library file into
`${XDG_DATA_HOME:-~/.local/share}/agents-projects-hub/stack-update/<sha256>/`
and points `current` at it. Run the copy with the system `python3`:

```bash
TOOL=~/.local/share/agents-projects-hub/stack-update/current/stack_update.py
python3 "$TOOL" status
python3 "$TOOL" plan --latest codex --set proxy=1.1.0 --out /home/example/stack-plan.json
python3 "$TOOL" apply /home/example/stack-plan.json --digest PLAN_DIGEST
python3 "$TOOL" rollback SWITCH_ID
```

Every command prints one JSON object. The exit code tells what happened:

| Exit code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | The switch failed and was restored, or restoration failed |
| 2 | Refused before any change |

Stage 3 runs `apply` and `rollback` as a transient user unit, so they survive a
restart of Hermes Gateway. Run by hand, they run in the foreground.

## Stop conditions

- **`restore_failed`:** a link or unit could not be put back. Inspect the switch
  record in `state_dir/switches/`, fix the component by hand, and do not start
  another apply until `status` shows the expected versions.
- **`refused: links changed`:** someone changed a link after planning. Make a
  new plan.
