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
exit, exec/spawn failure and descriptor retention. No Claude, inference,
account, network or live state is involved. The PR owns exact publication
evidence; these checks are not a native Claude witness.

## Next bounded witness

Reuse the pinned binary and isolated HOME/network/PID/IPC fixture. Independently
validate one consumed HTTP request per phase, exact full dialogue, selected
model/effort and the single saved UUID. Native interactive argv must deliberately
omit print-only options while retaining applicable restrictions; pin its shape.
Before `/exit`, require a bounded, strictly parsed saved-session update with the
exact fictional user/assistant pair, not merely a flushed server response. The
TUI must exit normally before cleanup and the final headless phase must validate
that pair in its own request. Do not retry discarded input, scrape screens or
send affirmative startup-dialog responses. Unavailable readiness remains a
failed witness. Missing UUID must not create a replacement or inference request.

This future witness does not establish production launch HOME/route continuity,
hook/MCP/plugin or local shell-escape isolation, human approvals, authority-data
custody, paid-fallback exclusion, Telegram or writer transfer. Keep transfer
refused until those independent launch-context, custody and transactional
lease gates pass. No Hub roles or advisor workflow are introduced.
