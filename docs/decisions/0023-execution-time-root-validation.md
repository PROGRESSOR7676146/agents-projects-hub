# ADR 0023: Revalidate the registered root before execution

Status: implemented; offline acceptance only

## Decision

REQ-ID-002 is an execution boundary, not just a registry-loading check. A cached
Project must still identify the same canonical directory under an unchanged
allowed root immediately before Hub starts or resumes productive provider work.
Following a replacement symlink into another allowed project is also refused.

One registry validator checks the cached binding, directory and allowlist,
then uses a bounded, local `git rev-parse --show-toplevel` to prove the intended
Git top-level. A `.git` marker alone is insufficient; real linked worktrees
remain supported. Inherited `GIT_*` variables cannot redirect this check.
An unavailable unrelated allowlist entry does not block a valid project.

External and embedded queue executors validate before project staging or
provider access. Refusal is terminal `failed` / `pre_execution`, with a fixed
`execution_root_invalid` code and a durable outbox notice. It neither consumes
context as a successful result nor retries the provider. Local inspection and
a new explicit request are the recovery path; Telegram cannot rebind a root.

Inline Codex, native direct/group execution and local-summary calls, managed
terminal preparation, `/local` resume-command preparation and the local pilot
use the same validation. Existing adopted origin/metadata/resume identity and
mode guards remain additional checks, not substitutes for it. Status, model
selection and Codex's model-free `/return` do not invoke this Git check merely
to change or display local routing state.

## Limits and evidence

This closes stale in-process registry trust; it does not introduce a filesystem
lock, a root/lane execution scheduler, or new persisted root identity. An
unmanaged CLI and Hermes's independent native Gateway execution remain outside
this executor boundary. Existing read-only Codex recovery retains its checkpoint
identity checks and never gains a productive fallback. A filesystem change
after validation is still a TOCTOU risk; provider sandboxing remains mandatory.

Tests first reproduced invocation after a registered root was relocated and
replaced by a symlink. Real SQLite/ingress/worker tests cover both queue modes
and all three locally managed runtimes, terminal notices without retry, inline
resume/local takeover refusal, and native direct-message/summary refusal.
Temporary real Git roots exercise invalid markers, nested directories, replaced
allowlists, linked worktrees and inherited Git environment isolation. Existing
execution fixtures now use actual empty Git repositories instead of markers.
Provider and Telegram boundaries are fictional; no live acceptance is claimed.

There is no schema change. Code rollback is possible without a data migration,
but restores the stale-root vulnerability and must not be called security
equivalent. This decision does not change session adoption or rollback policy.
