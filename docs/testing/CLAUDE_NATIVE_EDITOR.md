# Offline native Claude editor permissions

`tests.test_claude_native_editor` requires an explicitly supplied standalone
Claude 2.1.285 ELF, operator-verified supplied digest and pinned version, system Python 3.12 layout and usable
network/PID/IPC bubblewrap namespaces. No binary is downloaded or installed.

```bash
HUB_NATIVE_CLAUDE_FIXTURE_EXECUTABLE=/home/example/tools/claude-native \
  HUB_NATIVE_CLAUDE_FIXTURE_SHA256='<locally-verified-sha256>' \
  HUB_NATIVE_CLAUDE_FIXTURE_VERSION='2.1.285 (Claude Code)' \
  HUB_REQUIRE_NATIVE_CLAUDE_EDITOR_TESTS=1 \
  PYTHONPATH=src:. python -m unittest -v tests.test_claude_native_editor
```

The real PermissionServer, state and signed fictional receipt peer stay outside
the offline CLI namespace. Only its per-turn hook socket, disposable ordinary Git
project, copied ELF and readonly code/runtime are mounted. The production
file-tool argv keeps restricted mode, manual permissions, five tools and
`--permission-prompts none`; production ownership checks are never relaxed.
Fixture-only response/retry bounds use seven fixed local Messages responses.

Write Allow finishes the first native turn. Exact resume must include that Write,
its successful tool result and final text in the next request. Two CLI processes
run inside one fixture-owned Hub launch/job/daemon epoch. This does not prove a
fresh production launch or worker restart. A second Write to
the same file requires a fresh Deny; native Read precedes Edit Allow and another
Edit Deny. The host independently checks pending receipts and unchanged bytes
before each of four distinct decisions, then exact final bytes. Both turns must
finish successfully; native tool-error flags, actual POST/HEAD counts and zero
protocol violations are checked separately. Raw native/tool output is not kept.

Without opt-in this case skips; the required flag makes absence fail. This proves
pinned native hook invocation/enforcement against fictional decisions, not real
Telegram approval provenance, worker recovery, installed immutable-runtime or
host-wide authority custody, stop, quota, or CPA subscription/no-paid-fallback.
Those gates remain in the [file-tool runbook](../operations/CLAUDE_FILE_PERMISSIONS.md).

## Recorded offline evidence

The explicit native/focused run at source
`7e05c4d9c923de779b7084dcf677f6d02bddb151` passed 33 tests with no skips,
including the editor case and protected permission-host roundtrip. It used the
opt-in variables above, standalone CLI 2.1.285, system Python 3.12 and usable
bubblewrap namespaces with a local deterministic Messages peer and fictional
signed receipts. Fixture/test bytes are unchanged at published source
`e5dc14830ee76215c19566f4afc36397687da257`; the publication PR records the run.
Ordinary canonical runs can skip this optional native case and do not establish
that explicit-run evidence. Glob/Grep, outside-root native probes and the full
production launch are not proved by this narrow fixture.

`tests.test_process_namespace_topology` separately checks the production builder
with a narrow system runtime. Its real namespace denies unlink, rename,
replacement, chmod and child creation on synthetic runtime paths and system
aliases, while `/bin/sh` still executes. Project writes follow the selected
access mode; HOME/tmp remain writable, and runtime/`.git` writes and private
authority access remain blocked. Required namespace CI runs this module without
availability skips. It proves the temporary OS boundary, not an installed native
Claude runtime or a human Telegram decision.
