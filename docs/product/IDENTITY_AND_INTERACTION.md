# Identity and interaction requirements

This normative module is part of the
[product requirements baseline](PRODUCT_REQUIREMENTS.md).

## 6. Project, group, topic, and session identity

- **REQ-ID-001 (Implemented):** Telegram input MUST select only an immutable
  locally registered `project_id`; it MUST NOT supply a filesystem path.
- **REQ-ID-002 (Implemented):** Every project root MUST resolve canonically
  beneath a configured `allowed_root` and MUST be validated as the intended Git
  root before a session starts or resumes.
- **REQ-ID-003 (Implemented):** A project group MUST be identified by numeric
  `chat_id`; names and invite links are not authorization or binding.
- **REQ-ID-004 (Implemented):** A topic MUST be identified by numeric
  `(chat_id, message_thread_id)`. Renaming a topic MUST NOT change its identity.
- **REQ-ID-005 (Implemented):** Sessions, visible context, routing, and writer
  state MUST remain isolated between projects and topics.
- **REQ-ID-006 (Implemented):** Unknown, disabled, mismatched, or unbound
  identities MUST fail closed and require local confirmation; Telegram MUST NOT
  perform a filesystem rebind.
- **REQ-ID-007 (Accepted):** Each configured agent MAY maintain its own provider
  session in a topic. Switching agents MUST NOT silently merge provider-native
  session state.

Publishable examples use fictional identities only:

| Project ID | Canonical root | Telegram display |
| --- | --- | --- |
| `example-project` | `/home/example/projects/example-project` | Example Project |
| `disabled-example` | `/home/example/projects/disabled-example` | Disabled Example |

Real project names, roots, chat IDs, and topic IDs are private deployment state
and MUST NOT be recorded in this repository.

## 7. Routing and agent behavior

### Active and satellite routing

- **REQ-ROUTE-001 (Implemented):** Each topic MUST have exactly one active agent
  for ordinary human messages.
- **REQ-ROUTE-002 (Implemented):** A satellite invocation MUST NOT change the
  active agent unless the owner explicitly changes it.
- **REQ-ROUTE-003 (Implemented):** A single central group ingress MUST receive
  allowlisted human project-group updates and choose the target locally.
- **REQ-ROUTE-004 (Implemented):** Locally managed OpenCode and Antigravity
  group pollers MUST remain disabled; their own bot tokens MAY send responses so
  provider identity remains visible.
- **REQ-ROUTE-005 (Accepted):** Hermes retains its native Gateway and independent
  Telegram channel while its project-topic admission and visible-context
  exchange fail closed through the Hub integration.

### Message semantics

Routing precedence is deterministic:

1. **Real Reply — Implemented.** A Telegram Reply to a known bot-authored
   message routes exclusively to that author agent. A protocol-level reply to
   the forum topic root is only a topic anchor and MUST NOT be interpreted as
   user-selected Reply addressing.
2. **Explicit mention — Implemented.** An explicit provider bot mention routes
   to that provider.
3. **Forwarded message — Implemented.** A Telegram message carrying modern or
   legacy forward metadata is persisted as passive visible quoted context. Its
   text never enters command, emergency-stop, mention, or productive-turn
   parsing; the next explicitly productive user message may refer to it.
4. **Selected/pasted quote — Implemented.** A manually selected Telegram quote
   or pasted quotation is context for the active agent; it is not Reply
   addressing.
5. **Ordinary message — Implemented.** All other admitted text routes only to
   the topic's active agent.

- **REQ-ROUTE-006 (Implemented):** Non-target provider runtimes MUST NOT be
  invoked and MUST NOT consume model tokens.
- **REQ-ROUTE-007 (Implemented):** Transport delivery to an idle bot identity,
  if caused by Telegram Privacy Mode, MUST stop before provider invocation.
- **REQ-ROUTE-008 (Accepted):** Privacy Mode is a stable deployment setting and
  MUST NOT be toggled on agent switches. The central ingress must be able to see
  ordinary human group messages; satellite identities SHOULD retain privacy
  when they do not poll groups.
- **REQ-ROUTE-009 (Implemented behind queue mode):** Consecutive productive
  messages from one admitted user with the same numeric topic, target agent,
  provider session, model, and effort MUST be persisted independently but MAY
  be collected into one provider turn during a bounded quiet window. A command,
  explicit Reply/mention routing change, maximum batch window, or payload bound
  closes the batch. An unaddressed continuation inside the quiet window inherits
  the open batch target, including when the first part addressed a satellite
  provider rather than changing the topic's active provider.
- **REQ-ROUTE-010 (Implemented for socket-backed Codex; deterministic fallback
  elsewhere):** Input arriving after a normal Codex turn starts SHOULD use
  provider-native `turn/steer`. Explicit rejection returns it to FIFO; an
  ambiguous steering outcome becomes `indeterminate`. A runtime without safe
  same-turn steering MUST receive the input as one subsequent FIFO turn rather
  than dropping, simulating, or screen-scraping it.

### Telegram interaction contract

- **REQ-UX-001 (Implemented):** Every productive provider turn MUST identify
  Telegram as its user-facing transport. A new provider-native session, or any
  existing session that has not acknowledged the current contract version,
  receives the full interaction contract. The version is acknowledged only
  after a successful provider turn; later turns receive a bounded reminder.
- **REQ-UX-002 (Implemented):** The shared contract MUST prefer concise,
  outcome-first conversational replies, focused clarification when missing
  context materially affects the result, separate copyable blocks, restrained
  emoji, and visible progress without exposing hidden reasoning. Runtime notes
  MAY refine presentation but MUST NOT change safety or approval authority.
- **REQ-UX-003 (Implemented for private-chat admission and external queue
  refresh):** Private chats SHOULD show Telegram's native ephemeral `Thinking…`
  draft while productive work is pending. The same draft ID MUST be refreshed
  rather than creating transcript messages. Forum groups continue to use the
  bounded `typing` chat action; group drafts remain planned until Telegram
  exposes equivalent Bot API support.
- **REQ-UX-003A (Accepted platform boundary):** Receipt ticks are owned by
  Telegram. Ordinary bots have no Bot API method to force a read receipt;
  `readBusinessMessage` applies only to connected business accounts. The Hub
  MUST NOT imitate a receipt with an emoji reaction.
- **REQ-UX-004 (Implemented):** Long visible output MUST be delivered as ordered,
  replyable Telegram messages without truncation. Code or text intended for
  copying MUST occupy a separate copyable block. Durable multipart delivery
  retries from the first undelivered part. Queue-backed project turns persist
  each bounded HTML part and its Telegram message ID; legacy immediate-delivery
  paths use the same HTML-aware ordered splitter without queue durability.
- **REQ-UX-005 (Implemented for Hub-owned project and direct-message turns):**
  Generated user-facing artifacts MUST be published as
  Telegram attachments only from the exact project-contained staging directory
  `.hub/staging/<job_id>`. Hub MUST validate canonical path, regular-file status,
  per-file and aggregate size, extension, filename safety, and secret-like names,
  then copy each accepted file into a private immutable delivery spool. A fixed
  attachment-count cutoff MUST NOT silently discard otherwise valid work. Outbox records MUST bind
  the spool path, size, SHA-256 digest, and display name; delivery MUST revalidate
  those fields and remove the spool copy only after Telegram acceptance. Shared
  staging directories, archive formats, symlinks, and natural-language path
  mentions MUST NOT authorize an attachment. Rejected staged files MUST produce
  a bounded visible notice.
- **REQ-UX-006 (Planned):** Small closed decisions MAY render as bounded inline
  buttons whose opaque callbacks are persisted and scoped to the originating
  topic, provider session, and owner. Model-provided callback commands MUST
  never execute directly.
- **REQ-UX-007 (Planned):** A complex reversible task MAY enter a durable grace
  period after publishing its interpretation and intended approach. The default
  grace period is 60 seconds with Start, Clarify, and Cancel controls. Simple
  tasks start immediately; destructive work or new authority never starts only
  because a timer expired. Restart, free-text correction, and emergency stop
  MUST preserve deterministic behavior.
- **REQ-UX-008 (Accepted):** Telegram UI effects are transport capabilities, not
  model claims. The Hub owns files, reactions, buttons, message
  splitting, progress placeholders, and delivery confirmation. Providers own
  meaning, task execution, and the wording of visible results.

## 8. Shared visible context and spend policy

- **REQ-CTX-001 (Implemented):** Completed visible user/agent turns MUST be
  recorded in a bounded per-topic journal for active and satellite agents.
- **REQ-CTX-002 (Implemented; supersedes automatic handoff):** Ordinary turns
  MUST NOT receive another agent's journal automatically. `/context [agent_id]
  [1..20]` is an advanced, deliberately unadvertised command that invokes the
  selected provider with a bounded repeatable snapshot from the current numeric
  topic. Omitting `agent_id` selects prior turns from all other agents.
- **REQ-CTX-003 (Implemented):** Provider/model switches MUST only change local
  routing and session state. They MUST NOT invoke a model to summarize history,
  stage a handoff, advance a journal cursor, or spend provider tokens.
- **REQ-CTX-004 (Implemented):** Passive observation MUST NOT call a provider,
  force a response, or independently spend model tokens.
- **REQ-CTX-005 (Implemented):** Shared context MUST exclude hidden reasoning,
  raw tool output, raw terminal screens, credentials, private invite links, and
  environment dumps.
- **REQ-CTX-006 (Implemented):** When the user explicitly requests prior
  dialogue, the Hub MUST label it as lower-priority conversation context and
  preserve speaker/addressee attribution. Passive forwarded quotes remain a
  separate user-intent mechanism: they are delivered to the next addressed
  productive turn as quotes, never parsed as commands.
- **REQ-CTX-007 (Accepted):** A single user request SHOULD produce one provider
  turn unless the owner explicitly addresses multiple agents.

## 9. Provider identity and adapter contract

Every provider adapter MUST:

- use a stable agent ID and Telegram bot identity where configured;
- bind only to the canonical root selected from the local registry;
- create or resume provider sessions using structured IDs;
- construct subprocess arguments as arrays, never interpolate prompts into a
  shell command;
- preserve the configured sandbox/approval policy and reject bypass flags;
- return bounded visible output and structured session metadata;
- classify failures without exposing credentials or uncontrolled raw output;
- support an idle/busy boundary sufficient to prevent concurrent writers;
- fail visibly and in isolation so another provider/channel can remain usable.

Current provider status:

| Provider | Status | Product boundary |
| --- | --- | --- |
| Codex | Implemented | Persistent app-server thread; `workspace-write`; shared sockets use `on-request`, isolated stdio fallback denies escalation; metadata and usage status. |
| Hermes | Implemented integration | Native Gateway owns Telegram/session; Hub plugin/hook owns fail-closed project admission and bounded visible exchange. |
| OpenCode | Implemented adapter | Go-authenticated provider-owned session through structured CLI output; centrally routed bot identity. |
| Antigravity | Implemented adapter | `agy` conversation in sandboxed `accept-edits` work mode; no dangerous permission bypass. |
| Gemini CLI | Rejected for active product | Google provider work uses Antigravity; do not reactivate a parallel Gemini CLI path without a new decision. |

Provider bot identity maps to a runtime, not to a model or paid account. Model
selection belongs inside the agent session and should remain visible in status
or response metadata when the provider exposes it.
