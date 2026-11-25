# ADR 0113: Claude CLI store and unattended nightly promotion

Status: accepted policy (owner decision, 2026-11-25); implemented in source.
Release, installation and the first nightly promotion are separate.
Date: 2026-11-25.
Supersedes: the update part of [ADR 0048](0048-out-of-band-update-and-incident-plane.md)
(sections 4 and 5, for the Claude CLI used by Hub workers).
Amends: [REQ-OPS-015](../product/PERSISTENCE_AND_RECOVERY.md).

## Context

The Claude CLI reached Hub workers only inside a Hub release. `hub_deploy`
copied one configured binary into `releases/<sha>/bin/claude` and sealed it with
the release tree. A new CLI version therefore needed a new Hub release, and
Claude Code ships almost daily. Hub lagged the host CLI by more than ten patch
versions. Every release carried its own ~330 MB copy of the binary, and the
deploy input was a file in a cache directory.

The owner's host update tool updates the interactive CLIs every night with
digest checks and rollback. It must not edit Hub configuration. ADR 0048 had
planned a separate `stack-update` with an owner-confirmed plan for every apply;
it was never installed.

An independent architecture consultation (GPT Astra) recommended immutable
digest objects, one atomic pointer that selects both the binary and its
evidence, compatibility evidence per pair of Hub release and CLI digest, a
behavioral gate without a model call, and promotion only under an explicit
standing authorization. It advised against an elapsed-time soak; the owner
chose a 24-hour soak anyway, and the consultation's anti-starvation rule is
kept.

## Decision

### Standing authorization (owner, 2026-11-25)

The owner authorizes unattended nightly promotion of the Claude CLI into Hub
workers without a per-run confirmation, within these bounds:

- **Claude only.** Codex and the other providers keep their current paths.
- **Official source only.** A candidate is admitted only when its SHA-256 and
  size match the publisher's per-version release manifest, fetched over HTTPS
  from the official Claude Code download origin without redirects. The candidate is hashed
  as a copy inside the store before anything executes it.
- **Nightly window only.** Unattended promotion refuses outside the configured
  local window. Rollback is allowed at any time.
- **Soak of one day.** Hub records when it admitted each candidate, on its own
  clock, never from file times. Every new version becomes a candidate; a
  promotion takes the newest candidate that has aged one day and passed the
  gate, so daily CLI releases cannot starve promotion. One day is 24 hours
  with a one-hour allowance for nightly start jitter, so a candidate admitted
  last night is eligible tonight.
- **Gate per pair.** A candidate is promoted only after the model-free
  behavioral gate of the active Hub release passed for it.
- **Automatic rollback.** A failed check after the switch restores the
  previous selection and checks it again.

A model turn still cannot stage, promote or roll back. The command is run by
the host update tool, or by an agent on the owner's explicit request.

### Store and selection

The installation names one user-owned store directory. Its layout:

| Path | Content |
| --- | --- |
| `objects/<sha256>/claude` | One read-only copy per digest |
| `candidates/<sha256>.json` | Admission receipt: version, digest, size, source, admission time |
| `pairs/<hub_git_sha>-<sha256>.json` | Gate receipt for one Hub release and one digest |
| `quarantine/<hub_git_sha>-<sha256>.json` | Failed gate for a pair; not retried |
| `selections/<id>/` | Immutable: `selection.json` and a `claude` link to its object |
| `current` | The only mutable pointer: a link to one selection |
| `pending.json` | Present only while a switch is unfinished |
| `results/<run>.json` | One receipt per run |

Hub workers run `<store>/current/claude`. A switch replaces `current` with one
atomic rename, so the binary and its evidence change together. The worker
resolves the real path and checks the CLI's advertised controls before every
turn, and starts the resolved file, so a switch takes effect at the next turn
without a Hub restart, and a running turn keeps the file it started.
Configuration bytes do not change on a CLI switch.

The store serves only the `host` profile (ADR 0095), which runs the CLI outside
a namespace. Confined profiles mount trusted code only from sealed
release trees (REQ-SEC-008); for them `hub_deploy` refuses the store and the
release keeps its embedded binary. This keeps the confinement boundary
unchanged.

### Compatibility of a pair

Evidence binds a Hub release identity (its Git revision) to a CLI digest.
Every switch keeps the invariant that the active pair has evidence:

- a promotion runs the active release's gate for the candidate;
- a Hub deploy runs the new release's gate for the active CLI before the
  switch and refuses the deploy on failure;
- a Hub rollback to a store release refuses when that release has no evidence
  for the active CLI; the owner's agent rolls the CLI back first.

A minimum version is not evidence and is not used.

### Gate without a model call

The gate ships in every Hub release (`python -m hermes_codex_router.claude_cli_gate`),
so it tests that release's own code. It runs inside a `bwrap` namespace with its
own network namespace and an empty home, in which a fictional local Messages
API answers. It drives the production adapter of the host profile: production
argv, environment construction and stream parser. It requires:

- `--version` names the admitted version;
- the production capability check passes for the host and text-only controls;
- a new session completes with the expected result, session identity and usage;
- a session written by the previous CLI resumes under the candidate;
- an overloaded rejection ends as a classified terminal failure, not success
  or an unparsed stream.

The byte-exact native corpus stays a characterization of one version, not a
gate. Tool allow and deny effects, cancellation and background tasks are not
part of the first gate; the host profile runs with full rights by owner
decision and disables background tasks in its environment.

### Command

`scripts/hub_deploy.py` owns the store under the same lock as deploy, so a
promotion never races a deploy, rollback or another promotion:

| Action | Effect |
| --- | --- |
| `claude-status` | Read-only: active selection, previous, candidates and their eligibility, pair evidence, unfinished switch |
| `claude-promote [--candidate PATH --candidate-version V]` | Admit the candidate if new; then promote the newest eligible candidate |
| `claude-rollback` | Switch back to the previous selection after its pair check |

Exit codes match the host tool: 0 promoted, current or skipped; 1 failed and
the previous selection restored and verified; 2 needs inspection; 3 refused.
An unfinished switch found at start is completed or reverted before anything
else; when the result is undecidable the command returns 2 and changes nothing
more.

### Migration and retention

The first store release seeds the store from the sealed binary of the active
release, gates the pair and points the new worker configuration at
`current`. Older sealed releases stay untouched and keep their embedded
binaries, so a Hub rollback to them restores their original pair. New releases
no longer embed the binary, and deploy no longer reads a cache path.

Retention keeps the objects of the current and previous selection, of two
more recent selections and of candidates younger than three days, and prunes
the rest with their dangling receipts.

## Consequences

- Hub bots run a CLI at most about two days behind the host, without a Hub
  release, and each digest is stored once.
- A switch can reach a turn that starts between the switch and the
  post-check. The gate already ran on the same digest, so the post-check only
  confirms the pointer, the digest, the version and the advertised controls.
- A bad CLI that passes the gate reaches production for at most one night. The
  previous selection stays one command away.
- Trusting the HTTPS origin and the manifest checksum is weaker than
  verifying the manifest's publisher signature. Signature verification needs a
  pinned publisher key and is deferred.
- The host backup must capture the selection record and each object once. A
  restored record without its object is refused before use.
- Hub monitor drift reporting for the selection is not part of this slice.

## Rejected alternatives

- **No soak** (the consultation's recommendation): the owner prefers one day
  of interactive host use before bots receive a version.
- **Plain 24-hour soak of tonight's candidate:** daily releases starve it.
- **Rewriting the worker configuration per switch:** breaks external
  configuration pins and needs a restart.
- **Hashing the binary before every spawn:** costly and no boundary against the
  same user.
- **Owner-confirmed plan per apply (ADR 0048):** the owner explicitly wants no
  manual step for this component.
