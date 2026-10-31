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
input; selected/pasted quotations are context, not Reply addressing.

- **REQ-ROUTE-006 (Implemented):** Only targeted runtimes MAY be invoked or spend model tokens.
- **REQ-ROUTE-007 (Implemented):** Accidental transport delivery to an idle bot MUST stop before
  provider invocation.
- **REQ-ROUTE-008 (Accepted):** Agent switches MUST NOT toggle Privacy Mode; ingress must see
  ordinary messages, while non-polling satellites SHOULD retain privacy.
- **REQ-ROUTE-009 (Implemented behind queue mode):** Compatible consecutive messages MAY form one
  bounded turn, but MUST each be saved. Continuations inherit the open target; commands, routing
  changes and batch limits close it.
- **REQ-ROUTE-010 (Implemented for socket-backed Codex; deterministic fallback elsewhere):** Later
  Codex input SHOULD use safe native steering; explicit rejection returns it to FIFO and ambiguity
  MUST remain uncertain. Unsupported steering MUST produce a later FIFO turn without loss or
  simulation.

### Telegram interaction contract

- **REQ-UX-001 (Implemented):** Every productive turn MUST know it serves Telegram; each session
  receives the current full contract until a successful turn acknowledges it, then bounded
  reminders.
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
- **REQ-UX-006 (Planned):** Decision buttons MAY offer bounded choices, but callbacks MUST remain
  persisted and bound to the owner, topic and session; model-supplied commands MUST NOT execute
  directly.
- **REQ-UX-007 (Planned):** Complex reversible work MAY offer a durable 60-second
  Start/Clarify/Cancel grace period after explaining its approach; simple work starts immediately.
  Timer expiry MUST NOT authorize destruction or new authority, and restart, correction and stop
  MUST remain deterministic.
- **REQ-UX-008 (Accepted):** Hub owns actual Telegram effects and delivery confirmation; providers
  own meaning, execution and visible wording.
- **REQ-UX-009 (Implemented for Hub-owned durable queue ingress):** Every supported material MUST
  reach the selected project's exact root/session/turn as verified content, or both provider input
  and visible result MUST explain its absence.
  - Albums MUST form one bounded turn; late materials remain later FIFO work, never text steering,
    and file-only input gets a neutral instruction.
  - Quoted, forwarded or file content MUST NOT change routing, roots, policy, approvals or writers;
    forwards alone invoke nothing.
  - Unsupported, excessive or unavailable content MUST fail visibly; metadata alone is not reading,
    and the Bot API's 20 MiB file limit remains.
  - Claude images require explicit default-off opt-in, verified original bytes and disabled tools;
    protected file tools MUST NOT be combined with images.
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
  other agents' prior turns.
- **REQ-CTX-003 (Implemented):** Provider/model switches MUST change local state only, without model
  calls, summaries, handoffs, journal-cursor movement or token spend.
- **REQ-CTX-004 (Implemented):** Passive observation MUST NOT invoke providers, force responses or
  spend tokens.
- **REQ-CTX-005 (Implemented):** Shared context MUST exclude hidden reasoning, raw tools/screens,
  credentials, private invite links and environment dumps.
- **REQ-CTX-006 (Implemented):** Requested history MUST retain speaker/addressee attribution as
  lower-priority context; forwarded quotes reach the next addressed turn as data, never commands.
- **REQ-CTX-007 (Accepted):** One request SHOULD produce one turn unless the owner explicitly
  addresses several agents.

### Explicit Claude Code/Codex collaboration

- **REQ-COLLAB-001 (Retired by ADR 0065):** Hub does not manage or certify lead/advisor review;
  project instructions own it, with ordinary routing and session isolation unchanged.
- **REQ-COLLAB-002 (Retired by ADR 0065):** Retirement grants no sharing, invocation or replay
  authority; bots MUST NOT deliver bot-to-bot reviews and retired IDs MUST NOT be reused.

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
