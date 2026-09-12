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
It then creates a private forum supergroup, adds the Hub and all configured
provider bot usernames, grants only the Hub `invite_users`, `manage_topics` and
the Telegram `other` compatibility right, verifies forum mode and membership,
atomically adds the project to the local registry, and records the immutable
numeric group binding in schema 27 state. Provider bots remain ordinary members;
their group pollers are not enabled.

The completed binding is read dynamically by the Controller, so the new group
does not require a source-config edit. The registry remains the authority for
the canonical Git root. A state binding is admitted only when its project exists,
is enabled, and resolves to the exact recorded root.

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

The API hash and authorized Telethon session must be mode `0600`. The configured
`expected_user_id` must also appear in `owner_user_ids`. Bootstrap the session
interactively, inspect the returned numeric identity, pin that identity in the
configuration, then validate the whole Hub configuration:

```bash
agents-projects-hub project-provision-login /home/example/.config/agents-projects-hub/hub.json
agents-projects-hub validate-hub /home/example/.config/agents-projects-hub/hub.json
systemctl --user enable --now agents-projects-hub-project-provisioner.service
```

The login command is the only interactive credential bootstrap. The long-running
worker requires the identity to be pinned and refuses another authorized user.

## Failure and recovery

Local preparation is idempotent. A worker lost while preparing the root may
retry that step. A worker lost during group creation or bot configuration moves
the workflow to an unknown state and sends a bounded Hub notice; it never creates
or deletes another group automatically.

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
confirmation, exact user identity, Git preparation, bot configuration calls,
numeric binding, registry persistence, completion notification, and no blind
retry after unknown creation. They use fictional adapters and do not establish
Telegram behavior.

A deployment is accepted only after the exact clean revision is reported by the
Controller, sender, provider workers and project provisioner, followed by one
owner-driven live creation canary that verifies:

- the resulting chat is a private forum supergroup owned by the intended user;
- the Hub is present with Manage Topics and can receive ordinary owner messages;
- every configured provider bot is present and can send through its own identity;
- the new numeric chat maps to the intended project and exact canonical root;
- restart preserves the binding and ordinary, mention and Reply routing;
- no provider or LLM is invoked by the control-plane wizard itself.

Live identifiers, invite links, session files, access hashes and screenshots
remain private deployment evidence and must never enter the repository.
