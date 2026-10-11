# Offline native Claude editor permissions

`tests.test_claude_native_editor` requires an explicitly supplied standalone
Claude 2.1.285 ELF, pinned digest/version, system Python 3.12 layout and usable
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
its successful tool result and final text in the next request. A second Write to
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

