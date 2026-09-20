# Testing and acceptance strategy

## Automated gate

Run:

```bash
python scripts/validate.py
```

The gate performs the repository privacy scan, formatting, Ruff, Pyright,
unit/integration tests, release-lock verification, documentation/release
metadata contracts, and publishable configuration validation. Automated tests
use fake transports and temporary Git/SQLite fixtures. They must not contact
real Telegram groups or consume provider tokens.

GitHub CI and tag-release validation both call the same reusable
`.github/workflows/validate.yml` matrix for Python 3.11, 3.12, and 3.13. Each
matrix entry installs `.[dev]`, including the test-only Telegram client used by
the acceptance-actor unit tests, prepares an optional external public-author
declaration from a repository Actions variable, and runs this full canonical
command. The value enters through the step environment and is written without
output to a temporary `0600` file outside the checkout. It is not a secret or a
security boundary against candidate code running as the same runner user.
When the variable is absent, including in a fork or reusable-workflow context
where it is unavailable, no declaration is set and the required canonical
privacy scan runs without an exception; history that needs it remains red. The
release publication job depends on the entire reusable validation job and alone
has `contents: write`. `tests/test_workflows.py` parses the workflows
structurally, including GitHub's `on` key, and has negative temporary-copy cases
for a missing dependency, bypass condition, or Python 3.13. That offline
contract test rejects skipped/error-tolerant validation as well. A temporary
Git fixture executes the revision guard with matching and mismatched event,
checkout and annotated-tag commits without publishing a release. These tests
prove local wiring and guard behavior; a successful hosted Actions run remains
separate evidence.

`tests/test_fault_injection_matrix.py` is the subprocess queue acceptance gate.
It uses marker-synchronized fictional child actors, bounded parent waits, and
forced process termination to join real Controller polling, SQLite recovery,
isolated workers, and the standalone sender. Lower-level state-machine tests
remain in their focused modules.

## Privacy gate

`python -m hermes_codex_router.privacy_scan . --history` scans both the proposed
tree and every reachable Git blob plus commit/tag metadata. It rejects:

- files under `docs/history/` or `docs/handoffs/`;
- non-example email addresses, home paths, bot usernames, Telegram chat IDs,
  private invites, and bot tokens;
- raw agent/session transcript markers;
- local configuration, database, key, socket, session, and log files;
- private deployment fingerprints retained only as one-way hashes.

False positives must be resolved by using conspicuously fictional fixtures, not
by allowlisting real deployment data. The sole external policy declaration
permits one exact public author email only in its original author-email byte span
after strict merge structure and pinned GitHub signature verification. It does
not suppress body, trailer, file, credential, path, invite or other rule matches.
The fingerprint rule alone may be suppressed on the complete author-name or
hosted source-owner byte span when it exactly equals the valid local origin
owner; case variants, substrings, whitespace, lookalikes and all other display
names remain scanned. The scanner accepts zero or one final LF in the declaration
without stripping, case-folding or Unicode normalization; an absent, malformed,
linked, non-owner, non-`0600`, oversized or in-checkout file gives no exception.
Regression coverage creates a real temporary Git replacement mapping and proves
that history reads keep the original object bytes; the scanner also recomputes
each SHA-1 object ID before any metadata exception is applied.
An isolated canonical-history fixture combines a fictional declaration with
real temporary Git objects and a mocked successful signature result to test
policy wiring only. Pinned-key cryptographic verification has separate tests;
the mock is not evidence that a fixture was cryptographically signed.

## Live acceptance boundary

Live Telegram acceptance is a separate owner-coordinated operation because it
changes external state and uses real identities/accounts. Store its transcript,
IDs, account hints, screenshots, and service logs outside Git. Public status may
state only the reusable behavior tested and the kind of acceptance required.
The reusable go/no-go sequence and rollback boundary are defined in
[`LIVE_CANARY.md`](../operations/LIVE_CANARY.md).

## Publication preflight

Repository maintainers can install the versioned pre-push hook after creating
the external author-policy file used by the canonical privacy scan:

```bash
HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE=/home/example/.config/agents-projects-hub/public-author-policy \
  PYTHONPATH=src .venv/bin/python -m hermes_codex_router.publish_preflight --install
```

The installation records only the private file path in local Git configuration
and copies `.githooks/pre-push` into a mode-`0700` directory under the shared
Git common directory. It also records the validated Python executable used for
installation so linked worktrees do not require separate virtual environments.
The configured hook therefore covers every worktree, including a branch that
predates the versioned hook. When per-worktree Git
configuration is enabled, installation updates and verifies each registered
active worktree's effective hook path so a stale override cannot bypass the
shared hook. Git entries explicitly marked `prunable` are ignored because their
worktree directories no longer exist. The copied file is local Git state and
must be refreshed by running `--install` after a hook update.

Before a push, the hook captures the current worktree root and unsets every
repository-local environment variable reported by
`git rev-parse --local-env-vars`. Nested Git commands in the canonical test
suite therefore operate on their explicit temporary repositories rather than
the caller's index, object database or worktree. The hook then requires a clean
checkout and requires every published ref to resolve to the checked-out `HEAD`,
validates the external declaration through the canonical
scanner rules, and compares it exactly with the one named GitHub repository
variable without printing either value. It then prepends the current checkout's
`src` directory to `PYTHONPATH` and runs `python scripts/validate.py`. This
prevents an editable environment from another worktree from silently supplying
the validator implementation.

The hook fails before publication when GitHub or the repository variable is
unavailable. It reduces avoidable hosted-CI retries; it cannot promise that a
remote runner, dependency service, or network will remain available, and Git's
explicit `--no-verify` option can bypass a local hook. Exact-SHA hosted checks
remain the publication evidence. Forks and reusable callers without the
repository variable retain the fail-closed behavior described above.

## Dedicated acceptance user

Telegram bots never receive messages sent by other bots, so a service bot cannot
impersonate the operator for live E2E. An optional MTProto user actor covers the
bounded, non-destructive baseline while remaining restricted by Hub to one exact
canary topic.

Install the optional client and prepare private deployment files outside Git:

```bash
python -m pip install -e '.[e2e]'
agents-projects-hub e2e-validate PRIVATE_ACTOR_CONFIG
agents-projects-hub e2e-login PRIVATE_ACTOR_CONFIG
agents-projects-hub e2e-run PRIVATE_ACTOR_CONFIG
```

Copy `config/acceptance-actor.example.json` only to a private location and set
mode `0600`. Store only the hash value in a sibling file named
`telegram-api-hash`; that file and the generated session must also be mode
`0600`, and the artifact directory must be mode `0700`. Add the same user/chat/topic
triple to the private Hub configuration under `acceptance_actors`. Never commit
the copied config, session, identifiers, or result files.

For the first login only, `expected_user_id` may be omitted. `e2e-login` prints
the authenticated numeric user ID locally; immediately add it to both the actor
config and the matching Hub `acceptance_actors` entry. `e2e-validate` and
`e2e-run` fail closed until that identity is pinned.

Treat the canary topic as exclusive for the duration of a run. The runner fails
fast when it observes in-topic traffic from a sender outside the pinned actor,
Hub identity, and configured provider identities. Re-run only after the topic is
quiet; an interrupted or contaminated artifact is not acceptance evidence.
The runner stops after its first failed check; diagnose and drain that bounded
scenario before starting another run.

The optional `p0_p1_live` check is the disruptive seven-result P0/P1 suite.
Add `state_path` (an absolute mode-`0600` live SQLite file) and
`allow_service_restart: true` only for an owner-declared maintenance window,
align exactly one `provider_agent_ids` entry to `codex`, and set
`timeout_seconds` to 120–600. The actor requires the standard Controller and
Codex worker to be active, uses only fixed service names and prompts, restores
the initially active units on failure, and writes no live evidence to Git.
Successful repository tests prove this control flow only; `e2e-run` against a
specific deployed revision is the live evidence.

The `model_menu` check follows the complete callback ladder in the dedicated
topic: it selects the first provider, first model, and first effort exposed by
the Hub, then requires the deterministic final confirmation. This changes only
the canary topic's active session and never sends a productive model prompt.
The `reply_route` check first obtains a response through an explicit provider
mention, then sends a real Telegram Reply without another mention and requires
the response to come from the original provider identity.
The `forwarded_quote` check forwards a harmless provider marker back into the
topic, verifies that the forward alone receives no provider answer, then checks
that it is visible to the next explicit turn as quoted context.

The `burst_route` check sends one harmless instruction as three concurrent
Telegram API requests and requires one coherent provider answer. The
`stop_route` check must appear after `model_menu`: it targets only the first
provider selected there, starts a harmless wait, sends deterministic `stop`,
requires the Hub acknowledgement, and proves that a new turn works afterward.
