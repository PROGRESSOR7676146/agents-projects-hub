# Testing and acceptance strategy

## Automated gate

Run:

```bash
python scripts/validate.py
```

The gate performs the repository privacy scan, formatting, Ruff, Pyright,
unit/integration tests, and publishable configuration validation. Automated
tests use fake transports and temporary Git/SQLite fixtures. They must not
contact real Telegram groups or consume provider tokens.

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
by allowlisting real deployment data.

## Live acceptance boundary

Live Telegram acceptance is a separate owner-coordinated operation because it
changes external state and uses real identities/accounts. Store its transcript,
IDs, account hints, screenshots, and service logs outside Git. Public status may
state only the reusable behavior tested and the kind of acceptance required.
The reusable go/no-go sequence and rollback boundary are defined in
[`LIVE_CANARY.md`](../operations/LIVE_CANARY.md).

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
