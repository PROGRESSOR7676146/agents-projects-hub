# ADR 0023: deterministic saved-session connection

Status: accepted  
Date: 2026-09-12

## Context

ADR 0022 established exact saved Codex-thread adoption, immutable origins,
replacement guards, and a positive Telegram activation boundary. Its local
administrative interface required operators to supply project, chat, topic and
prior Hub-session identifiers, then send `/return`. That protocol is safe but
too error-prone for the normal mobile path.

The Controller cannot discover saved conversations by reading arbitrary local
files or by calling a model. It also cannot hold a SQLite transaction across an
app-server or Telegram request. A button callback's old message ID is not a new
input boundary, and retrying a Telegram send after an unknown outcome may create
two irreversible visible effects.

## Decision

Schema 26 stores one `session_connect_workflows` state machine for all entry
points. It binds the owner, optional selected project, canonical root, exact
source thread, destination, expected prior Hub session, expiry, stage, result,
and optional code. Separate tables retain bounded opaque candidates/options,
workflow delivery records, one-time code digests, and per-owner failed-code
windows. Telegram callbacks contain only opaque row IDs.

The supported entrances are:

1. `/connect` in a registered project topic;
2. `/connect` in the owner-only Hub private control plane, with project and
   existing/new destination selection;
3. local `session connect [CONFIG]`, which lists supported app-server metadata
   for one registered root and issues `/connect CODE`.

The existing `session attach-codex` command remains an administrative recovery
interface. A new conversation still starts through ordinary first input, and
`/new` remains the explicit reset.

Discovery runs only in the isolated Codex worker. It uses the installed
app-server `thread/list` contract with exact `cwd`, OpenAI provider, supported
interactive `cli` or `vscode` source, non-archived state, descending update
time, and a hard result limit. Labels use
only a safe bounded name when available, UTC update time, and a short thread-ID
suffix. Transcript previews, first prompts and filesystem paths never enter
Telegram. A final `thread/read` metadata request rechecks exact source/root and
idle state without resuming or starting a turn.

Activation uses these boundaries:

1. Owner confirmation changes the durable workflow to `activation_requested`.
2. The external worker performs the metadata recheck and prepares one neutral
   marker outbox row. No binding has changed.
3. The standalone sender publishes the marker to the destination topic.
4. Only its positive Telegram `message_id` permits one immediate SQLite
   transaction to invoke the ADR-0022 attachment primitive, archive the expected
   old binding when required, record the activation/context floor, transfer the
   writer to Telegram, consume a claimed code, complete the workflow and marker
   receipt, and prepare the success outbox row.
5. Productive admission accepts only messages later than the marker. Success is
   delivered after the transaction, so no separate `/return` is needed.

Any recheck or transaction failure rolls back the whole local transition. If
the sender cannot prove whether Telegram accepted the marker, the marker and
workflow become `unknown`; the prior binding remains current and the marker is
not sent again automatically. A stale sender lease is recovered with the same
conservative classification.

Forum-topic creation is also a one-shot external effect. The workflow records
`creating_topic` before the Bot API call. A proven rejection fails safely. A
network or invalid-response ambiguity becomes `topic_create_unknown`, tells the
owner to inspect the registered group, and never creates another topic
automatically. Telegram never creates or binds a local project.

Codes contain ten characters from an ambiguity-reduced random alphabet. SQLite
stores only SHA-256, owner, project/root/source, model/effort, expiry, claim and
result. Invalid attempts are limited per owner. Redemption claims at most one
workflow; cancel releases an unconsumed claim. Consumption occurs only in the
activation transaction, and a post-success repeat returns the recorded result.

## Consequences

The normal path no longer exposes numeric Telegram or Hub session identifiers.
Private Hub text is a deterministic control plane and cannot route to a
provider. Project and group creation remain local/onboarding concerns; the Hub
menu only states that this future action is unavailable.

The workflow depends on external Codex workers and the standalone Hub-capable
sender, matching the adoption execution policy. Schema-25 binaries reject schema
26. Runtime rollback therefore needs an immutable artifact that supports schema
26; deleting workflow/origin evidence or relabelling the database is forbidden.

Repository tests prove state, adapter and synthetic transport boundaries with
fictional data. They do not prove bot permissions, the installed Telegram
client UI, or provider-history continuity in a live deployment.

## 2026-09-25 amendment: discovery during a productive turn

The original worker loop processed connection metadata only after productive
work. A long turn could outlast the workflow and leave the initial `/connect`
acknowledgement without a selection menu. The Codex worker now runs a separate
metadata-only loop with its own SQLite connection and app-server client. It
leases the same durable workflow state as the idle worker loop, so duplicate
polls cannot duplicate selection output. It never starts or resumes a turn.
Expired worker requests receive one durable outbox notice; late metadata
responses cannot change the expired state.
