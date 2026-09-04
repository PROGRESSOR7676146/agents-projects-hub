# Project instructions

This repository contains only the reusable orchestration product. It must not
contain information about any operator's real projects or deployment.

## Read order

1. `docs/product/PRODUCT_REQUIREMENTS.md`
2. `docs/status/PROJECT_STATUS.md`
3. `docs/INDEX.md`
4. `docs/SECURITY.ru.md`
5. only the architecture, decisions, operations, and tests relevant to the
   assigned change

Before substantial work, read the optional private operator profile at
`${XDG_CONFIG_HOME:-$HOME/.config}/agents-projects-hub/USER.md` when it exists.
It is local context, never repository content: do not quote, copy, summarize,
stage, or commit it. Repository behavior must remain useful without it.

## Collaboration stance

- Treat the operator as a product partner and decision owner, not an infallible
  specification source. A suggestion starts exploration; it becomes a
  requirement only after consequences and alternatives are understood.
- Say plainly when an idea adds needless latency, state, coupling, fragility,
  security risk, or maintenance cost. Explain the concrete failure mode and
  recommend a simpler alternative. Do not silently implement a weak idea out
  of deference.
- Preserve the operator's control with outcome-first explanations, explicit
  boundaries, and honest acceptance evidence. Never report a backend proxy
  transition as end-to-end success for an unrelated interactive client.
- Automate diagnostics and acceptance wherever practical. Do not push source
  inspection, code writing, repetitive terminal work, or manual testing onto
  the operator merely because it is convenient for the agent.
- Develop underspecified ideas proactively: research relevant supported
  mechanisms, identify prior art and operational constraints, propose concrete
  options, and ask only questions whose answers materially change the result.
- Distinguish useful disagreement from obstruction. Once an informed decision
  is made and is safe and authorized, execute it decisively.

The `docs/history/` and `docs/handoffs/` directories are forbidden. Never commit
conversation exports, live acceptance transcripts, real project names, account
identifiers, bot usernames, numeric deployment IDs, owner-specific paths,
private invite links, local provider rollouts, configuration, credentials,
state databases, or deployment screenshots. Use only conspicuously fictional
examples (`example.com`, `/home/example`, and documented placeholder IDs).

Run `python -m hermes_codex_router.privacy_scan . --history` before every commit. The same
privacy gate is mandatory in `scripts/validate.py` and CI. Do not bypass it or
add an allowlist entry for real deployment data; fix the fixture or prose.

For behavior changes, update the product requirements or project status when
observable behavior, scope, acceptance, or lifecycle classification changes.
Record durable consequential rationale under `docs/decisions/`. Run the
narrowest relevant checks followed by `python scripts/validate.py` when
practical; distinguish automated coverage from owner-driven live E2E.

Never call a deployment current or accepted without naming the exact clean Git
revision and confirming that every required long-running component reports that
revision. Process liveness, a clean development tree, package version, and a
passing test from another revision are not substitutes. Use the evidence levels
defined in `docs/operations/ENGINEERING_BASELINE.md`; “all green” must state its
revision and highest proven level.

## Safety invariants

- Telegram input selects only an immutable `project_id`; never accept a filesystem path from chat.
- Resolve and validate every project root against local `allowed_roots` before starting or resuming a session.
- Construct subprocess argv as arrays. Never concatenate prompts, paths or session IDs into a shell command.
- Default Codex policy is `workspace-write` plus `on-request`. Never add `danger-full-access` or automatic approval.
- Hermes is not an approval authority. Codex/tlive retains approval ownership and first-valid-answer-wins behavior.
- A Telegram topic is keyed by numeric `(chat_id, message_thread_id)`; its title is display metadata, not identity.
- Never forward hidden reasoning, credentials, environment dumps or raw terminal screen contents.
- One active Codex turn per project lane. Parallel work requires an explicit additional worktree and lane.
- Persist routing state locally with restrictive permissions; treat duplicate Telegram updates idempotently.

Use test-first development for router behavior. Live bot changes, daemon launch, service installation and credential changes require an explicit deployment task.
