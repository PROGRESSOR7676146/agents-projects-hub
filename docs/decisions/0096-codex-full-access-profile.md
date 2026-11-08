# ADR 0096: Local Codex full-access profile

Status: accepted  
Date: 2026-11-09

## Context

The owner decided that routinely allowing every Codex approval makes the prompt
ineffective for this installation and chose full access without approval requests.
The existing default and all other profiles retain their previous behavior.

## Decision

Only `codex_permission_profile: "full"` in local Hub configuration selects
`danger-full-access` and `never` on both transports. Telegram and the project
registry cannot select a policy. Start/resume use the legacy `sandbox` selection;
turn/start uses `sandboxPolicy`. Start/resume must confirm the exact full sandbox
projection, approval policy, root and provider before a turn. Settings drift
aborts the turn through the existing fenced recovery path. Unexpected approval
requests under `never` are refused; no component answers Allow automatically.
Existing sessions retain their pinned selection and changing access requires `/new`.
Other managed profiles retain their existing verification and selection.

A named managed `full` is not used: the pinned server obtains managed profile
requirements only from `/etc/codex/requirements.toml`, with no user-level source.
That would require sudo, which routine operation forbids.

## Passive protocol evidence

The pinned `codex-cli 0.166.0` generated app-server JSON Schema without a model
call. In the generated `v2/ThreadStartParams.json` and
`v2/ThreadResumeParams.json`, `sandbox` references `SandboxMode`, whose enum
includes `danger-full-access`. `v2/TurnStartParams.json` exposes `sandboxPolicy`
with a `SandboxPolicy` variant of `{"type":"dangerFullAccess"}`.
`v2/ThreadStartResponse.json` and `v2/ThreadResumeResponse.json` project `sandbox`
as that same policy type. The runtime also accepts the documented legacy string
form. This is protocol evidence, not a live acceptance or proof that a running
server accepted a particular thread; the response is checked per thread.

## Consequences

Prompt injection in project content, or a compromised authorized Telegram account,
can cause actions with the OS user's filesystem and network privileges without a
further approval. The project allowlist routes work; it is not containment. Other
profiles retain their sandbox and human-only approval rules. Full access does not
make Hub or Hermes an approval authority. Doctor reports the local selection,
existing incompatible conversations, and states that access is verified per thread
at start/resume without calling a model. No database migration is needed for the existing named-profile
snapshots.
