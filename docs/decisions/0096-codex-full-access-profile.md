# ADR 0096: Local Codex full-access profile

Status: accepted  
Date: 2026-11-09

## Context

The owner decided that routinely allowing every Codex approval makes the prompt
ineffective for this installation and chose full access without approval requests.
The existing default and all other profiles retain their previous behavior.

## Decision

Only `codex_permission_profile: "full"` in local Hub configuration selects the
named managed profile `full`, with `danger-full-access` and `never` on both
transports. Telegram and the project registry cannot select a policy. The profile
must be the sole allowed managed default, and start/resume must confirm the exact
full sandbox projection, profile, approval policy, reviewer, root and provider
before a turn. All three RPCs select the profile and policy without a legacy
sandbox override. Settings drift aborts the turn through the existing fenced
recovery path. Any unexpected approval request under `never` is refused; no
component answers Allow automatically. Existing sessions retain their pinned
selection and changing access requires `/new`.

## Passive protocol evidence

The locally available `codex-cli 0.162.1` generated its experimental app-server
JSON Schema without a model call. `ThreadStartParams`, `ThreadResumeParams` and
`TurnStartParams` admit a string `permissions` and `approvalPolicy: "never"`;
the schema says not to combine named selection with the legacy sandbox field.
`ThreadStartResponse` exposes the active profile and a sandbox policy whose type
can be `dangerFullAccess`. This proves protocol shapes, not that a particular
installed or pinned server has a working `full` definition or resolves it to
that projection. The runtime checks refuse if managed metadata or the exact
response cannot confirm it; they never substitute another policy. Schema evidence
is not live acceptance.

## Consequences

Prompt injection in project content, or a compromised authorized Telegram account,
can cause actions with the OS user's filesystem and network privileges without a
further approval. The project allowlist routes work; it is not containment. Other
profiles retain their sandbox and human-only approval rules. Full access does not
make Hub or Hermes an approval authority. Doctor reports the local selection,
existing incompatible conversations and unconfirmed Codex configuration without
calling a model. No database migration is needed for the existing named-profile
snapshots.
