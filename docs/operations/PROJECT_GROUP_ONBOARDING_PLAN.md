# Project and Telegram-group onboarding

Status: implemented offline; deployment and live acceptance required
Last updated: 2026-09-13

The owner-only Hub private chat exposes **Create project** under `/projects`
when `project_provisioning.enabled` is true. The Hub owns the deterministic
wizard. A separate local worker, authenticated as an explicitly authorized
Telegram user, performs the user-only MTProto operations needed to create a
private forum supergroup and add bots. No model is invoked.

Telegram documents supergroup/forum creation through
[`channels.createChannel`](https://core.telegram.org/method/channels.createChannel)
and adding participants through
[`channels.inviteToChannel`](https://core.telegram.org/method/channels.inviteToChannel).
Both methods are user-only. A Bot API identity cannot provide the requested
automation by itself.

## User flow

1. In the private Hub chat, run `/projects` and choose **Create project**.
2. Enter the Telegram group display name.
3. Choose one configured `allowed_root`. The callback contains an opaque local
   option ID, never a path.
4. Enter a safe lowercase project ID. It is also the single direct-child
   directory name under the chosen root. An uppercase value is normalized to
   lowercase; separators, dots and absolute paths are rejected.
5. Review the exact group name, immutable project ID and derived local path,
   then choose **Create**.

The worker creates the directory when absent and initializes an empty directory
as a Git repository on branch `main`. It refuses a non-empty non-Git directory.
Before creating a group it verifies the pinned creator, every owner captured at
confirmation and every configured bot identity. It then creates a private forum
supergroup, invites every non-creator owner and bot, grants the non-creator
owners the documented administrator-rights set, grants only the Hub
`invite_users`, `manage_topics` and the Telegram `other` compatibility right,
and verifies forum mode, privacy, membership and rights. Only then does it
atomically add the project to the local registry and record the immutable numeric
group binding in schema 28 state. Provider bots remain ordinary members; their
group pollers are not enabled.

After the binding is committed, the standalone sender claims a durable task and
idempotently applies the
Hub project command scope (`/menu`, `/connect`, `/stop`) and clears project
command scopes for locally managed provider bots, matching existing groups.
It handles at most one group task per cycle, gives final results priority, and
persists retry and Telegram `retry_after` deadlines across restart. Failure in
one group does not block another eligible group and never repeats group creation.

The completed binding is read dynamically, so the new group does not require a
source-config edit. One resolver is used by Controller ingress, workers,
recovery, saved-session connection and command audit. The registry remains the
authority for the canonical Git root. A state binding is admitted only when its
project exists, is enabled, resolves to the exact recorded root, remains inside
an allowed root, and is the real Git toplevel.

Several confirmed workflows may wait in the durable queue, but provisioning is
globally serialized. A heartbeat extends the token-bound SQLite lease during
long RPCs. One nonblocking file lock beside the Telethon session covers login
and the full worker lifetime, so another process cannot use that identity even
if a paused process outlives its database lease.

## Private configuration

Install the `provisioning` extra and keep every credential outside Git. A
deployment-local example is:

```json
{
  "project_provisioning": {
    "enabled": true,
    "api_id": 12345,
    "api_hash_file": "/home/example/.config/agents-projects-hub/secrets/telegram-api-hash",
    "session_path": "/home/example/.local/state/agents-projects-hub/project-provisioner.session",
    "expected_user_id": 123456789,
    "group_about": "Private project group managed by Agents Projects Hub"
  }
}
```

The API hash and authorized Telethon session must be mode `0600`, use an actual
`.session` suffix, and live in a private directory; sidecars are also normalized
to private mode. The configured `expected_user_id` must also appear in
`owner_user_ids`. Every other configured owner must already be resolvable by the
technical account before creation. Bootstrap the session interactively, inspect
the returned numeric identity, pin that identity in the configuration, then
validate the whole Hub configuration:

```bash
agents-projects-hub project-provision-login /home/example/.config/agents-projects-hub/hub.json
agents-projects-hub validate-hub /home/example/.config/agents-projects-hub/hub.json
systemctl --user enable --now agents-projects-hub-project-provisioner.service
```

The login command is the only interactive credential bootstrap. The long-running
worker requires the identity to be pinned and refuses another authorized user.

## Failure and recovery

Local preparation is idempotent. A worker lost while preparing the root may
retry that step. Preflight and proven permission failures enter a locally visible
blocked state, retain the project/root reservation, and can resume after
correction. A worker lost during group
creation or bot configuration moves the workflow to an unknown state and sends
a bounded Hub notice; it never creates or deletes another group automatically.

Resume a blocked workflow only after correcting the named local or Telegram
condition. The confirmation records the current configured-owner snapshot:

```bash
agents-projects-hub project-provision-resume \
  /home/example/.config/agents-projects-hub/hub.json WORKFLOW_ID \
  --confirm WORKFLOW_ID
```

If a project command-scope task exhausts its bounded attempts, correct the
Telegram permission or transport cause and reset only that exact bot/group pair:

```bash
agents-projects-hub project-command-retry \
  /home/example/.config/agents-projects-hub/hub.json \
  --chat-id -1001234567890 --bot-identity hub \
  --confirm=-1001234567890:hub
```

After inspecting Telegram locally, resume an exact unknown workflow with the
numeric group ID and MTProto access hash. The confirmation must equal
`WORKFLOW_ID:CHAT_ID`:

```bash
agents-projects-hub project-provision-reconcile \
  /home/example/.config/agents-projects-hub/hub.json WORKFLOW_ID \
  --chat-id -1001234567890 --access-hash 123456789 \
  --confirm WORKFLOW_ID:-1001234567890
```

Reconciliation never accepts a path and rejects a group or project identity
already bound by another workflow. Existing groups, directories and registry
entries are retained on failure.

## Acceptance boundary

Offline tests prove bounded root derivation, opaque callbacks, duplicate
confirmation and expiry, exact user identity, two-owner preflight/promotion,
session permissions and lock exclusion, Git preparation, lease heartbeat,
numeric binding, registry persistence, project/root reservation, one-operation
command convergence, read-only menu audit, durable completion/menu delivery, and no
blind retry after unknown creation. They use fictional adapters and do not
establish Telegram behavior.

A deployment is accepted only after the exact clean revision is reported by the
Controller, sender, provider workers and project provisioner, followed by one
owner-driven live creation canary that verifies:

- the resulting chat is a private forum supergroup owned by the intended user;
- the other configured owner is present with the documented administrator rights;
- the Hub is present with Manage Topics and can receive ordinary owner messages;
- every configured provider bot is present and can send through its own identity;
- the new numeric chat maps to the intended project and exact canonical root;
- restart preserves the binding and ordinary, mention and Reply routing;
- no provider or LLM is invoked by the control-plane wizard itself.

With only the primary and technical owners, this is an owner-driven canary. It
does not prove the experience of an independent user without administrator
rights, and acceptance reports must state that limitation.

Live identifiers, invite links, session files, access hashes and screenshots
remain private deployment evidence and must never enter the repository.
