# Native Claude local-continuity preparation

Production Claude `/local` and `/return` remain refused. The next compatibility
witness must use the actual native interactive interface between two headless
invocations, preserving one disposable store, root, UUID and independently
expected fictional dialogue. Another `--print` invocation cannot establish TUI
continuity. The existing [selection corpus](CLAUDE_NATIVE_SELECTION.md) retains
its separate headless evidence.

## Controlling-terminal prerequisite

`tests.native_pty_capture` and its trusted `native_pty_exec` trampoline provide
only test-fixture process/terminal ownership. The trampoline acquires a
controlling terminal and verifies that stdin, stdout and stderr share its
device, that the leader owns its session and foreground process group, and
that terminal dimensions are 24 by 80. A separate bounded pipe reports this
pre-exec check; it is not provider acceptance or prompt-editor readiness.

The parent drains, counts and discards PTY bytes. It exposes no terminal bytes,
text callback, ANSI parser or screen-based synchronization. Independently gated
literal input steps transfer once with partial-write accounting; gates must be
prompt and use protocol/session evidence. Timeout, output overflow, a failed
gate or incomplete input fails the fixture. Complete writes prove PTY admission
only. Native consumption needs its own endpoint/session assertions.

Completion is observed with `waitid(WNOWAIT)` before cleanup. The leader's PID
remains reserved while the parent kills its owned group, then reaps and closes
every descriptor. Normal exit, nonzero exit and signal exit remain distinct;
EOF/EIO alone proves none of them. Trampoline exit 126 may report verified TTY
facts despite failed exec, and must not establish native success. The helper
requires an outer disposable PID namespace for descendants escaping its group;
ordinary process-group cleanup is not an isolation boundary.

Focused fake-process checks:

```bash
PYTHONPATH=src:. python scripts/validate.py --profile focused \
  tests.test_native_pty_capture
```

Tests use Python children and fictional temporary files. They cover actual
controlling-terminal properties, literal prompt/exit input, partial writes,
gate failure/deadline, output flood, leader/descendant cleanup, signal/nonzero
exit, exec/spawn failure, terminal closure while the leader remains alive and
descriptor retention. No Claude, inference,
account, network or live state is involved. The PR owns exact publication
evidence; these checks are not a native Claude witness.

## Next bounded witness

The standalone `tests.claude_saved_dialogue` oracle prepares the saved-session
gate without invoking a provider. The caller supplies one fixture-owned UUID
filename, exact canonical root and two, four or six alternating visible messages;
expectations are not derived from the arriving transcript. It returns only
`waiting`, `ready` or `invalid`, with no transcript, identity or raw error.
`waiting` uses the caller's existing finite polling deadline; `invalid` aborts.
Only endpoint response completion **and** `ready` may admit a later `/exit`.
The helper is not yet wired into a native interactive witness.

Every lexical ancestor is opened with directory descriptors and no symlink
following. A regular single-link file is checked before a bounded read of at
most 1 MiB plus one overflow byte. Limits are 32 path components, 512 completed
records and 16 KiB per record or trailing fragment. Strict JSON preserves
duplicate-key refusal and bounds depth, nodes, finite numbers and Unicode.
File identity, size and timestamps are rechecked after parsing; a second walk
checks ancestor and final-file identities. Missing or changed snapshots wait;
stable unsafe files or invalid records refuse. These checks observe a snapshot,
not filesystem custody or atomic isolation after the final check.

The synthetic contract permits one ordered `user`/`assistant`/`attachment` chain,
exact session/root/version and `isSidechain=False`. Attachments advance the
chain but cannot hide top-level dialogue. Only `queue-operation`, `atis-latch`,
`last-prompt` and `cost-state` metadata are recognized; metadata does not advance
the chain or contain dialogue/chain identity fields. Their full schemas and
attachment context equivalence remain outside this oracle. User content is one
exact string; assistant content is one exact text block. Unknown types,
branches, duplicate IDs, sidechains and wrong or extra dialogue refuse.
Completed malformed records refuse even with an incomplete suffix. A bounded
non-newline suffix always waits, including complete JSON without its newline.
This narrow chain contract is a synthetic restriction, not native compatibility.

Run its provider-free tests with the neighboring parser and PTY checks:

```bash
PYTHONPATH=src:. python scripts/validate.py --profile focused \
  tests.test_claude_saved_dialogue tests.test_claude_native_request_contract \
  tests.test_native_pty_capture
```

Reuse the pinned binary and isolated HOME/network/PID/IPC fixture. Independently
validate one consumed Messages POST per phase, exact full dialogue, selected
model/effort and the single saved UUID. Native interactive argv must deliberately
omit print-only options while retaining applicable restrictions; pin its shape.
The pinned CLI advertises a positional prompt in interactive mode. Supplying
one fixed prompt through that argv avoids an unsupported prompt-editor readiness
claim; it does not prove keyboard submission through the editor. Do not retain
print-only permission-prompt, output-format or turn-budget options in this phase.
Before `/exit`, require a bounded, strictly parsed saved-session update with the
exact fictional user/assistant pair, not merely a flushed server response. The
TUI must exit normally before cleanup and the final headless phase must validate
that pair in its own request. Do not retry discarded input, scrape screens or
send affirmative startup-dialog responses. Unavailable readiness remains a
failed witness. Missing UUID must not create a replacement or inference request.

The initial disposable startup investigation on CLI 2.1.285 established a
headless seed with one validated Messages POST, then observed one HEAD and zero
Messages POSTs during interactive resume. The PTY helper refused incomplete
input; the expected new pair was absent from the saved dialogue. This was a
failed compatibility investigation, not a three-phase witness or a diagnosis of
the specific startup barrier. No terminal bytes or affirmative startup answers
were used. Headless mode skips workspace trust, so a headless seed cannot prove
that interactive trust/onboarding is satisfied. Do not invent private settings
keys to bypass that boundary. The next witness needs documented disposable
startup preparation and a strict saved-dialogue validator before advancing.

This future witness does not establish production launch HOME/route continuity,
hook/MCP/plugin or local shell-escape isolation, human approvals, authority-data
custody, paid-fallback exclusion, Telegram or writer transfer. Keep transfer
refused until those independent launch-context, custody and transactional
lease gates pass. No Hub roles or advisor workflow are introduced.
