# Identity and interaction requirements

Part of the [product requirements baseline](PRODUCT_REQUIREMENTS.md).
Supporting detail: [reference](REQUIREMENT_DETAILS.md#identity-and-interaction).

## 6. Project, group, topic, and session identity

- **REQ-ID-001 (Implemented):** Telegram MUST select a registered immutable project ID, never a
  filesystem path.
- **REQ-ID-002 (Implemented):** Before start or resume, the project MUST be the intended canonical
  Git root beneath an allowed root.
- **REQ-ID-003 (Implemented):** Numeric chat ID MUST identify a group; names and invite links grant
  no authority.
- **REQ-ID-004 (Implemented):** Numeric chat and thread IDs MUST identify a topic; renaming MUST NOT
  change its identity.
  The displayed title MUST follow an authorized owner rename in a bound group and MAY start from
  the topic's creation name; it never selects a session or route.
- **REQ-ID-005 (Implemented):** Projects and topics MUST keep sessions, context, routing and writer
  state separate.
- **REQ-ID-006 (Implemented):** Unknown, disabled, mismatched or unbound identities MUST refuse work
  until local confirmation; Telegram MUST NOT rebind roots.
- **REQ-ID-007 (Accepted):** Each agent MAY have its own topic session; switching MUST NOT merge
  native sessions silently.

Real deployment names, roots and numeric identities MUST stay outside this repository.
Published examples MUST be fictional.

## 7. Routing and agent behavior

### Active and satellite routing

- **REQ-ROUTE-001 (Implemented):** Each topic MUST have one active recipient for ordinary human
  messages.
- **REQ-ROUTE-002 (Implemented):** Calling a satellite MUST NOT change that recipient without the
  owner's explicit choice.
- **REQ-ROUTE-003 (Implemented):** One central group ingress MUST route allowlisted human input
  using local code.
- **REQ-ROUTE-004 (Implemented):** OpenCode and Antigravity group pollers MUST stay disabled; their
  bots MAY send responses.
- **REQ-ROUTE-005 (Accepted):** Hermes MUST retain its independent Gateway/channel and refuse
  project admission or context exchange when Hub checks fail.

### Message semantics

Routing follows this order: real Reply to a known bot author, explicit provider
mention, then ordinary input to the active agent. A topic-root reply is only an anchor.
Forwards are passive quoted context and never commands, stops, mentions or productive
input; selected/pasted quotations and the replied-to message's text are context, not Reply
addressing (ADR 0111).

- **REQ-ROUTE-006 (Implemented):** Only targeted runtimes MAY be invoked or spend model tokens.
- **REQ-ROUTE-007 (Implemented):** Accidental transport delivery to an idle bot MUST stop before
  provider invocation.
- **REQ-ROUTE-008 (Accepted):** Agent switches MUST NOT toggle Privacy Mode; ingress must see
  ordinary messages, while non-polling satellites SHOULD retain privacy.
- **REQ-ROUTE-009 (Implemented behind queue mode):** Compatible consecutive messages MAY form one
  bounded turn, but MUST each be saved. Continuations inherit the open target; commands, routing
  changes and batch limits close it.
- **REQ-ROUTE-010 (Implemented for socket-backed Codex; opt-in Claude source, live acceptance
  pending; deterministic fallback elsewhere):** Later Codex input SHOULD use safe native steering;
  explicit rejection returns it to FIFO and ambiguity MUST remain uncertain. Unsupported steering
  MUST produce a later FIFO turn without loss or simulation.
  - Explicit default-off Claude streamed input MAY deliver one compatible text-only FIFO successor
    through the same owned external process, without images; later, material, explicit-context,
    handoff, retry and leading-`/` inputs MUST retain ordinary FIFO, because the native CLI runs
    streamed slash text as its own command. `/goal` in a Claude topic MUST be refused without a
    job: its native session Stop hook would inject continuations the Hub cannot own.
  - A rejected stream event MUST keep only its protocol shape and input counts in the private
    job detail, never its text, identifiers or tool names; policy refusals stay generic.
  - Process, session, root, tool profile, permission host and writer ownership MUST stay with the
    parent until complete stream and cleanup settle; tools MUST NOT be interrupted to simulate
    steering.
  - The child MUST be durably reserved before transmission and never requeued after that fence.
    Exact processing receipts MUST bind inputs to their covering result without becoming native
    turn IDs or claiming that the model followed the correction.
  - Success MUST require every submitted input's receipt and covering successful result, successful
    process exit and cleanup; raw completion MUST be saved before atomic parent result/outbox and
    child settlement. A later receipt MUST await its later result, and the first result closes
    further correction admission.
  - Missing/conflicting evidence, persistence failure and mixed/failed results MUST retain submitted
    inputs, uncertainty and root exclusion without replay. Exact confirmed owner stop MAY cancel
    the invocation and submitted child; unreserved input remains FIFO.

### Telegram interaction contract

- **REQ-UX-001 (Implemented):** Every productive turn MUST know it serves Telegram; each session
  receives the current full contract until a successful turn acknowledges it, then bounded
  reminders.
  - Runtimes with interim delivery (Codex, Claude) MUST be asked for an early message stating
    how the request was understood (goal, reasons, context, scope, approach) and for progress at
    milestones and at least every ten minutes; Claude texts followed by a tool call or a newer
    native request MUST be submitted to the progress outbox, subject to its rate limit and
    supersession, and the final answer MUST NOT be submitted as progress (ADR 0099).
- **REQ-UX-002 (Implemented):** Replies MUST be concise, outcome-first and copyable, with restrained
  emoji and visible progress but no hidden reasoning. Materially missing audience, facts, format or
  language MUST prompt one focused question before drafting; presentation notes cannot change safety
  or approvals.
- **REQ-UX-003 (Implemented for private-chat admission and external queue refresh):** Pending
  private-chat work SHOULD refresh one ephemeral Thinking draft; groups use bounded typing actions
  until equivalent support exists.
- **REQ-UX-003A (Accepted platform boundary):** Telegram owns read receipts; Hub MUST NOT imitate
  them with reactions.
- **REQ-UX-004 (Implemented):** Long output MUST arrive in ordered, replyable parts without
  truncation, with separate copyable blocks. Durable delivery MUST resume at the first undelivered
  part; legacy splitting does not claim durability.
- **REQ-UX-005 (Implemented for Hub-owned project and direct-message turns):** Attachments MUST come
  only from the exact job's project staging area, pass path/type/name/secret/size checks, and retain
  a private immutable, verified delivery copy until Telegram accepts it. Symlinks, archives, shared
  staging and prose paths MUST NOT authorize files; rejected files must be explained and valid files
  cannot disappear behind a count cutoff.
- **REQ-UX-006 (Implemented for project topics, ADR 0097):** Decision buttons MAY offer bounded
  choices, but callbacks MUST remain persisted and bound to the owner, topic and session;
  model-supplied commands MUST NOT execute directly. A press MUST act only as the sender's ordinary
  reply with the label.
- **REQ-UX-007 (Planned):** Complex reversible work MAY offer a durable 60-second
  Start/Clarify/Cancel grace period after explaining its approach; simple work starts immediately.
  Timer expiry MUST NOT authorize destruction or new authority, and restart, correction and stop
  MUST remain deterministic.
- **REQ-UX-008 (Accepted):** Hub owns actual Telegram effects and delivery confirmation; providers
  own meaning, execution and visible wording.
- **REQ-UX-010 (Implemented, ADR 0110; default-off; owner live-accepted 2026-11-23):** With the Hub bot, external
  queue/outbox and a local opt-in, a configured owner MAY open one status panel per registered
  project topic with `/panel`; `/panel off` stops updates and a repeated `/panel` replaces it. Hub
  edits that one message instead of sending status messages.
  - The snapshot MUST use only durable Hub evidence, without model calls, transcripts or invented
    progress: a headline by fixed priority (owner-needed durable blocker, stop requested, local
    writer, executing or queued, idle), the job's agent and model, time since start, queue and held
    counts, and the last observed event by kind with its time.
  - Agent activity, interim delivery and process observation MUST stay distinct; process liveness
    MUST NOT count as activity. Silence MAY be shown as an observation, never as a stop or by itself
    as a reason to intervene; "needs owner" MUST come only from a durable blocker with a supported
    action.
  - The only control is an owner-only read-only «Обновить» callback bound to the exact Hub bot,
    chat, topic, message and current panel instance; a stale or foreign press MUST change nothing
    and say the panel is outdated. Panel commands and callbacks are handled before routing and MUST
    NOT become prompts.
  - Panel sends and edits MUST rank below every mandatory delivery and notice, happen only when the
    displayed content changes, respect Telegram cooldowns and keep per-operation evidence. Unknown
    creates or edits MUST NOT be retried automatically, and a late receipt MUST NOT revive a
    replaced or disabled panel. An unknown edit may have applied: the last confirmed payload stays
    separate and neither version is claimed as shown.
  - Panel-only outcomes MUST NOT change jobs, queues, holds or provider health, or raise
    provider-delivery overdue alerts; a genuine shared Telegram transport failure still follows the
    existing sender-health rules.
  - The panel is group-visible: it MUST show only allowed status fields, never prompts, tool
    arguments, paths, raw errors or approval text.
- **REQ-UX-009 (Implemented for Hub-owned durable queue ingress):** Every supported material MUST
  reach the selected project's exact root/session/turn as verified content, or both provider input
  and visible result MUST explain its absence.
  - Albums MUST form one bounded turn; late materials remain later FIFO work, never text steering,
    and file-only input gets a neutral instruction.
  - Replied-to, quoted, forwarded or file content MUST NOT change routing, roots, policy,
    approvals or writers; forwards alone invoke nothing.
  - The replied-to message's bounded text, author kind and named non-text content, and each burst
    part's own selected quote, MUST reach the turn as labelled quoted data; a forward arriving
    while the tail turn still collects its burst MUST join it (ADR 0111).
  - Unsupported, excessive or unavailable content MUST fail visibly; metadata alone is not reading,
    and the Bot API's 20 MiB file limit remains.
  - Claude images require explicit default-off opt-in, verified original bytes and disabled tools;
    protected file tools MUST NOT be combined with images. The unconfined `host` profile MAY carry
    images in the initial frame of its streamed input channel, with `claude_stream_input` enabled
    and the same bounds and replay check (ADR 0102).
  - Success MUST require an exact caller/session receipt retaining supported image bytes, prompt,
    markers and image count/order; processing may transform bytes, so receipts prove neither pixel
    equivalence nor understanding.
  - Missing or conflicting processing evidence MUST retain materials, uncertainty and root exclusion
    without replay.

## 8. Shared visible context and spend policy

- **REQ-CTX-001 (Implemented):** Completed active and satellite dialogue MUST remain in a bounded
  per-topic journal.
- **REQ-CTX-002 (Implemented; supersedes automatic handoff):** Sharing MUST be explicit through the
  unadvertised `/context [agent_id] [1..20]` command using a bounded repeatable current-topic
  snapshot; ordinary turns receive no automatic journal injection. Omitting the agent selects all
  other agents' prior turns. Agent letters (REQ-CTX-008) reach a turn only through the owner's
  press, never as journal sharing.
- **REQ-CTX-003 (Implemented):** Provider/model switches MUST change local state only, without model
  calls, summaries, handoffs, journal-cursor movement or token spend.
- **REQ-CTX-004 (Implemented):** Passive observation MUST NOT invoke providers, force responses or
  spend tokens.
- **REQ-CTX-005 (Implemented):** Shared context MUST exclude hidden reasoning, raw tools/screens,
  credentials, private invite links and environment dumps.
- **REQ-CTX-006 (Implemented):** Requested history MUST retain speaker/addressee attribution as
  lower-priority context; forwarded quotes reach the still-collecting or next addressed turn as
  data, never commands.
- **REQ-CTX-007 (Accepted):** One request SHOULD produce one turn unless the owner explicitly
  addresses several agents.
- **REQ-CTX-008 (Implemented, ADR 0112; default-off; owner live-accepted 2026-11-24):** With the Hub
  bot, external queue/outbox and owner-configured directed routes between exact numeric topics,
  an agent MAY send one bounded text letter per final answer to a routed topic.
  - Addresses MUST be per-turn, bound to the whole route and offered only for the source topic's
    routes. The request MUST be the exact last line outside code fences naming one staged UTF-8
    `.md`/`.txt` file of at most 64 KiB; anything else MUST keep the answer and say that the
    letter was not sent.
  - The letter MUST commit with the source result and its exact route, and appear in the target
    topic only as a Hub-bot document with an owner-only «Обработать» button while that route and
    both endpoint bindings still hold. Only a receipt carrying the document confirms the display;
    an unknown display MUST NOT be resent, and a rejected or unknown one MUST be reported once in
    the source topic.
  - A letter MUST NOT start, steer or join a turn, reach an agent passively, or change routing,
    roots, policy, approvals or writers. Only the configured owner's press on the exact displayed
    letter, while its topic still belongs to the target project, MAY admit it, once, as the
    owner's ordinary message with the letter attached, to the topic's current agent.

### Explicit Claude Code/Codex collaboration

- **REQ-COLLAB-001 (Retired by ADR 0065):** Hub does not manage or certify lead/advisor review;
  project instructions own it, with ordinary routing and session isolation unchanged.
- **REQ-COLLAB-002 (Retired by ADR 0065):** Retirement grants no sharing, invocation or replay
  authority; bots MUST NOT deliver bot-to-bot reviews and retired IDs MUST NOT be reused. Agent
  mail (REQ-CTX-008, ADR 0112) transports owner-processed letters, not reviews or obligations.

## 9. Provider identity and adapter contract

Every adapter MUST preserve stable identity, registered canonical roots, structured
session IDs, sandbox/approval policy and one writer. Subprocess arguments MUST be
arrays, never shell interpolation; bypass flags MUST be rejected. Output and metadata
MUST be bounded, and failures MUST be visible, isolated and free of secrets/raw output.

Codex retains persistent sessions and safe official transports; Hermes retains its
native Gateway; OpenCode and Antigravity retain structured, sandboxed adapters.
Claude remains text-only by default with explicit CPA/subscription routing and
separately accepted tool opt-ins under REQ-AUTH-009 and REQ-SEC-008; no implicit tools,
MCP, skills or permissions. Gemini CLI remains Rejected without a new decision.

Bots represent runtimes, not models or accounts. Observable model selection should
remain visible in status or response metadata.
