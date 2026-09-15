# ADR 0024: user-authorized project-group provisioning

Status: accepted and implemented offline  
Date: 2026-09-13

## Context

The owner wants `/projects` in the private Hub chat to create a local project
and its private Telegram forum group, including bot membership and permissions.
Telegram's Bot API can manage an existing forum but cannot create the required
supergroup. The MTProto `channels.createChannel` and `channels.inviteToChannel`
methods are explicitly user-only. Pretending a bot can perform those operations
would leave an unavoidable manual gap; hiding a general user-account client
would silently expand authority.

Telegram must still never select an arbitrary filesystem path. Group titles are
mutable display metadata and cannot authorize project or group identity. A
network interruption during group creation can leave a real group even when the
client receives no result, so automatic retry can create duplicates.

## Decision

The Hub remains the owner-only deterministic UI and queues one durable schema-27
workflow. It accepts a group display name, an opaque option for one configured
`allowed_root`, and a safe project ID. The local root is derived as exactly one
direct child of that root. Path text, separators, dot components and forwarded
input are not accepted as authority.

An independently managed `agents-projects-hub-project-provisioner` process owns
the Telegram user session. This authority is disabled by default. Its API hash
and Telethon session are private mode-`0600` files outside Git, and its numeric
user ID must be pinned to a configured owner before the worker starts. It never
reads bot tokens or invokes a model.

For one workflow, the worker:

1. creates or validates the derived root and initializes an empty directory as
   a Git repository;
2. creates a private forum supergroup through the pinned user identity;
3. adds the Hub and locally managed provider bot identities; providers declared
   `managed_externally` retain their native admission boundary and are not a
   provisioning prerequisite;
4. grants the Hub only invite, topic-management and compatibility rights, while
   provider bots remain ordinary members;
5. verifies forum mode and membership;
6. idempotently writes the registry entry and commits an immutable numeric
   `project_id`/`chat_id`/canonical-root binding;
7. queues a bounded private Hub result notice.

SQLite transactions end before every network operation. A lost worker before a
network call may repeat local preparation. A lost or ambiguous group-create or
bot-configuration call becomes `unknown` and is never retried automatically.
Recovery requires a local exact workflow, numeric chat ID and access-hash
confirmation. No rollback deletes a group, directory or registry entry.

The Controller admits a completed dynamic binding only if the current registry
contains the same enabled project at the exact recorded canonical root. Existing
static configuration bindings remain compatible.

## Consequences

The owner sees one Hub wizard, while Telegram user-only authority stays explicit
and isolated. Successful onboarding needs no hand edit of the Hub source config.
The deployment gains one required long-running component whenever provisioning
is enabled, and live acceptance must report its exact Git revision alongside the
Controller, sender and provider workers.

Full automation now depends on a locally authorized Telegram user session and
Telegram account limits. Privacy Mode and BotFather policy remain deployment
settings; the provisioner cannot relax them. Unknown outcomes require operator
inspection because duplicate avoidance is more important than automatic retry.
Optional membership for an externally managed recovery provider remains an
operator action and cannot block creation of the Hub-owned project group.
