# ADR 0028: Explicit Codex provider routing without replacing session identity

Status: accepted

## Decision

The optional local `codex_model_provider` setting names a configured Codex model
provider. With no setting, existing routing and OpenAI-only discovery remain
unchanged. With a setting, metadata discovery accepts `openai` plus that exact
provider, and thread start/resume explicitly pins the configured route. A
returned provider mismatch fails before productive inference.

Schema 30 stores the actual inspected provider in new immutable origins and in
the durable connect marker workflow. Existing origins are not rewritten: they
describe provenance, not the current HTTP destination. Execution permits an
origin's original provider or the explicitly selected route before resume, then
requires the selected route after resume. Root, thread ID, writer ownership and
first activation remain immutable. No unknown provider becomes authorized by
being present in a saved file.

`/local` includes provider and model overrides when routing is explicit, so
persisted settings cannot silently redirect an older thread. `/return` remains
a model-free lease change. Explicit routing also disables replacement-thread
fallback: an unavailable exact session fails closed instead of transferring a
summary to a new conversation. Shared sockets preserve human approval ownership;
isolated stdio still denies escalation.

## Deployment and rollback

Configure the named provider and its credential source in Codex before enabling
the Hub setting. Do not put credentials or provider endpoints in Telegram.
Retain a separate direct launcher. Other runtimes and their credentials are
unaffected. Do not restart a shared app-server under live CLI writers.

Before schema migration, prepare a consistent backup and a distinct clean
schema-30-compatible rollback artifact. Runtime rollback retains current state;
older schema-29 binaries cannot safely open it. The compatibility-only artifact
does not accept proxy origins for productive execution and fails closed for
those sessions; use the current route-capable artifact for routing recovery.

Offline tests cover both original OpenAI and custom-provider origins through
attach, return, productive fictional work, local transfer, restart and return,
plus mismatched metadata and transactional migration failure. They are not live
Telegram or provider-inference acceptance.
