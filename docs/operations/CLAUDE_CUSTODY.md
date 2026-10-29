# Claude scoped installation checklist

Status: finite trusted-installation preparation; installed/native acceptance open.
Contract: [REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).
Scope amendment: [ADR 0072](../decisions/0072-trusted-personal-installation.md).
Runtime, mount, key, tlive and native permission procedures remain in the
[file-tool runbook](CLAUDE_FILE_PERMISSIONS.md).

## Stopped global study

ADR 0072 supersedes the former whole-host launch/service inventory, UID/VM
selection and exclusion of every same-UID helper as mandatory editor gates.
The earlier finding remains true: an unconfined same-UID process can read
mode-0600 keys and forge symmetric receipts. It is an accepted limitation, not
an isolation pass or an unfinished requirement of this installation. Do not
repeat a global inventory to start the editor; no new launcher is needed.

## Finite evidence bundle

Prepare privately; no real roots, identities, credentials or receipts in Git.
Check valid existing grants before requesting uncovered live effects. A source
check, generated manifest or completed checklist does not authorize deployment.

| Item | Required evidence and stop condition |
| --- | --- |
| Candidate and consumers | Exact clean revision, immutable artifact/dependency/tlive hashes and current schema; every required Hub component reports that revision. Include idle Hermes Gateway as a source/schema consumer. |
| Compatible rollback | Distinct clean runtime supporting retained current-schema state, coordinated Hermes source/environment pin, release manifest and offline rehearsal. Never put an older DB over accepted work. |
| Supported launch | Exact installed CLI/digest and namespace/runtime/filesystem applicability; ordinary disposable Git root, separate session HOME, read-only `.git` and runtime. Unsupported linked worktree or failed boundary refuses before invocation. |
| Private paths and endpoints | Existing descriptor/mount/private-path negative checks; keys/state and operations-drain endpoint absent from confined provider/hook views. Filesystem IPC visibility is limited to the exact per-turn permission socket; shared networking leaves loopback/abstract sockets reachable. Check the confined launch's reachable authority listeners, including FD transfer/signing/management; unknown authority-bearing exposure blocks activation. Use fictional sentinels with positive controls at intended authority locations, never real keys or destructive controls. No host-wide same-UID exclusion claim. |
| Identity and recovery | Canonical root, native session/mode/home, generation, lease and unresolved jobs; SQLite-consistent backup plus matching retained spool/history/config. Preserve uncertainty and original delivery evidence. |
| Acceptance scope | One disposable root/topic, finite native calls/effects/waits, four fresh human Write/Edit Allow/Deny decisions, stop/restart/stale tests, result/delivery recovery and cleanup ownership. Three roots belong to separate core acceptance. |
| Subscription | Private CPA upstream attestation and installed CLI→CPA→subscription observation; no paid/API/extra-usage fallback. The exposed client credential must grant no CPA management or host-control capability. Loopback configuration or a review call is insufficient. With `auth_route: subscription` (ADR 0082) see [subscription route](#subscription-route) instead. |

No automatic reset, replay, reconciliation or approval is used to make the gate
green. Any unknown required result remains open with its exact evidence limit.

## Automated offline rehearsal

Run the focused profile and strict namespace scenarios on a supported host:

```sh
python scripts/validate.py --profile focused \
  tests.test_claude_custody_rehearsal tests.test_workflows
HUB_REQUIRE_NAMESPACE_TESTS=1 python -m unittest -v \
  tests.test_claude_file_sandbox.ClaudeFileSandboxTests.test_namespace_denies_private_symlink_git_write_and_host_paths \
  tests.test_claude_permission_host_roundtrip.PermissionHostRoundtripTests.test_namespace_client_preserves_peer_gate_and_atomic_allow_deny \
  tests.test_claude_custody_rehearsal.ClaudeCustodyRehearsalTests.test_namespace_blocks_authority_aliases_and_privilege_but_shares_network
```

These fixtures use fictional keys/state and no inference or Telegram. They test
private/symlink/proc/cwd/descriptor access, Git/runtime writes, capabilities,
project/session positive controls and signed hook/SQLite Allow/Deny. Existing
mount-role guards detect project/HOME and ordinary Git inode aliases, including
pre-existing descendant directory intersections; they do not freeze contents.
Provider-neutral extraction and sealed-material primitives remain useful shared
source; retired advisor work is not revived.

The custody rehearsal intentionally demonstrates reachable loopback and abstract
Unix listeners, including fictional descriptor transfer. That success is an
exposure witness, not a passed denial test. The scoped installed check must
exclude authority endpoints from the confined launch; it does not require an
inventory proving isolation of every trusted unconfined provider. Developer-mode
skips are not namespace evidence; strict required fixtures must actually run.

## Installed acceptance

1. Recheck exact revision/schema, consumers, leases/queue/outbox and unresolved
   outcomes through compatible read-only tooling. Do not release roots or alter
   uncertain history for a canary. Verify backup/rollback before cutover.
2. Apply only the authorized candidate/configuration using the existing release
   procedures. Verify immutable runtime/hooks, namespace applicability and
   descriptor closure on the exact installed kernel/bubblewrap/CLI. Run all three
   strict namespace scenarios above on that installed kernel/bubblewrap. Use
   fictional sentinels at intended authority locations and positive fixture
   controls so an absent target cannot pass as denial; never probe real keys or
   destructive controls. Record listeners reachable from this confined namespace
   and verify that no authority-bearing listener yields private files/FDs, signing,
   management or host control without a secret unavailable to it. Check the CPA
   client credential cannot perform management/host control. Unknown authority
   exposure stops activation; this is a scoped point-in-time check, not proof
   of all future trusted host launches. Retain the
   separate native-terminal, physical-exit and durable-history checks for any
   maintenance drain; an ACK is not permission to stop.
3. Run the bounded native/human scenarios in the file-tool runbook on the exact
   installed CLI/digest and fresh production launch. Confirm Read/Glob/Grep,
   fresh Write/Edit Allow/Deny, exact resume, identity, stop, restart, stale/
   duplicate refusal and result/delivery recovery. Synthetic receipts are not
   human acceptance. Automate deterministic checks, never the human decisions.
4. Confirm the actual CPA or direct subscription route and required component revision
   convergence. Record offline, scoped installed, native and human/Telegram
   evidence separately. The accepted same-UID/admin risk cannot become a pass.

## Subscription route

For `auth_route: subscription` ([ADR 0082](../decisions/0082-claude-subscription-route.md)):

1. The owner runs `claude setup-token` and writes
   `CLAUDE_CODE_OAUTH_TOKEN=<token>` to
   `~/.config/agents-projects-hub/claude-subscription.env` with mode 0600. Only
   the Claude worker units read this file. Hub never puts the token in
   configuration, prompts, logs or Telegram.
   To give a project its own account, the owner runs `claude setup-token`
   while signed in to that account, adds
   `CLAUDE_CODE_OAUTH_TOKEN_<SUFFIX>=<token>` to the same file and sets
   `"subscription_token_by_project": {"<project_id>": "CLAUDE_CODE_OAUTH_TOKEN_<SUFFIX>"}`
   on the Claude agent. Other projects keep the default token.
2. Remove `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_API_KEY`
   from the environment of the Claude workers; the preflight refuses them. Hub
   launches load no user, project or local Claude settings, but admin-managed
   settings still apply: confirm they define no `env` credential, provider
   selector or `apiKeyHelper`. Confirm that the worker's Claude home holds no
   stored login, so nothing can stand in for a rejected token.
3. The owner confirms that extra usage is off on every account.
4. Set `"auth_route": "subscription"` on the Claude agent and restart only the
   Claude workers through the service manager. `developer` is refused on this
   route; `editor` and `workstation` are allowed.
5. On the exact deployed revision, record the CLI version and, for every
   Claude worker unit, its effective environment files and Claude home. Claude
   turns run only in these queue workers; the provider service refuses Claude.
   Accept one text-only, one editor and one workstation turn, a new and a
   resumed turn in each bound project and in a default project with the account
   identified, an injected API key refused before launch, and a turn with a
   deliberately invalid token failing rather than succeeding. A limit on one
   account must end that turn with a limit notice, not move it to the other.

Rollback: remove `subscription_token_by_project`, set `"auth_route": "cpa"`,
take `claude-subscription.env` out of the Claude workers' environment (the CPA
route refuses any subscription token), restore the CPA variables, validate the
configuration and restart the Claude workers. A bound project then runs on the
CPA account; pause it instead if that is unacceptable. Renew each token from its
own account before its one-year expiry. A worker reads the tokens once at start,
so a changed token takes effect only after a Claude worker restart.

## Stop, rollback and closure

Unknown effects or a failed scoped check stop promotion. Keep text-only/default
behavior and existing uncertain evidence. Staging must not create another live
poller or writer. After authorized cutover, stop new admission and preserve
executing/indeterminate work before selecting the compatible rollback runtime.
Never restart the old environment until the new pollers/writers have physically
stopped and ownership has been independently checked. Do not restore an old
database, automatically replay a task or silently copy sessions across homes. Keep artifacts and receipts private. Key rotation is a
separate credential task.

Close only the named finite installed/native/human checks at the exact revision;
editor closure is owned by the [matrix](CLAUDE_LEAD_REVIEW_PLAN.ru.md).
Global infrastructure selection and hostile-same-UID isolation are not pending.
