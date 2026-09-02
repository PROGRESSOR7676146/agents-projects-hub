# ADR 0003: Scoped Telegram acceptance actor

Status: accepted
Date: 2026-08-31

## Context

Bot API identities cannot perform a faithful end-to-end test of another bot:
Telegram does not deliver messages sent by bots to other bots. Synthetic update
fixtures prove deterministic routing, but they do not prove live Telegram
delivery, forum-topic metadata, response identity, or inline keyboards.

Making a test user a global owner would create unnecessary authority. A stolen
test session could then send productive requests in every configured project.
Live transcripts and Telegram deployment identifiers also cannot belong in the
public repository.

## Decision

Live unattended acceptance uses a dedicated Telegram user through MTProto. The
Hub configuration authorizes that user only for one exact supergroup and forum
topic. The actor is not accepted in direct messages or any other topic; owners
retain their existing global authorization.

The client is an optional dependency. Its separate mode-`0600` configuration,
API hash, and session file, plus its mode-`0700` result directory, live outside
Git. The checked-in runner exposes only a fixed set of bounded checks:

- deterministic `/status` and `/accounts` commands;
- the complete `/model` provider/model/effort selection using the first offered
  option at each step;
- an optional fixed no-tools connectivity prompt to explicitly allowlisted
  provider usernames;
- a three-part burst that proves a rapidly sent instruction is handled as one
  conversational turn;
- real Telegram Reply provenance and passive forwarded-quote semantics; and
- a bounded emergency-stop and immediate recovery check against only the first
  provider selected by the preceding `model_menu` check.

The runner cannot accept arbitrary commands or prompt text from its config.
Exact results are written as private mode-`0600` artifacts. Public project
status may record only aggregate pass/fail state.

## Consequences

- Live Telegram behavior can be tested without asking the owner to type every
  canary message.
- Obtaining an application API ID/hash and performing one interactive account
  authorization remain deployment-local prerequisites.
- The bounded actor supplements synthetic and operator tests; destructive
  commands, account rotation, restart faults, quote semantics, and writer
  transfer still require separately controlled acceptance procedures.
- Compromise of the test account is constrained to its configured canary topic,
  but the account must still be removed or its session revoked when unused.
