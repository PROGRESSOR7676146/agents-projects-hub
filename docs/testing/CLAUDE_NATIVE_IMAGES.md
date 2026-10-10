# Offline native Claude image continuation

`tests.test_claude_image_session` is a separate test-only compatibility corpus
for the explicitly pinned CLI 2.1.285. Production Claude still rejects image
materials. Run it manually with a locally verified standalone ELF:

```bash
HUB_NATIVE_CLAUDE_FIXTURE_EXECUTABLE=/home/example/tools/claude-native \
  HUB_NATIVE_CLAUDE_FIXTURE_SHA256='<locally-verified-sha256>' \
  HUB_NATIVE_CLAUDE_FIXTURE_VERSION='2.1.285 (Claude Code)' \
  HUB_REQUIRE_NATIVE_CLAUDE_IMAGE_TESTS=1 \
  PYTHONPATH=src:. python -m unittest -v tests.test_claude_image_session
```

The same disposable empty-root network/PID/IPC boundary runs a fresh session
with fictional PNG bytes and a second process resuming its exact saved UUID with
fictional JPEG bytes. Only the new caption and base64 image enter structured
stdin; prior dialogue is restored by the CLI. The independent endpoint contract
requires exact decoded images, captions, roles, ordering, prior image/assistant
history, native annotations and system scaffolds before serving either response.
All other request fields retain the full native text-corpus validation. CLI-added
temporary image annotations are expected compatibility data, never input paths
or file-tool authority. Init capabilities must explicitly report empty tools,
MCP, plugins and skills. The real stream reader/parser must save both expected
visible results under the chosen UUID and model.

Each invocation has its own bounded endpoint, fully closed with handlers joined
before the next process starts. Positive invocations allow one Messages POST and
one optional strictly validated passive HEAD. A third process requests a known
nonexistent UUID: it may issue only that passive HEAD, must deliver all stdin and
exit naturally, and must return one strict structured missing-session failure.
Malformed, duplicate or conflicting terminal data, success under another UUID,
any attempted POST, unknown request, timeout or transcript mutation fails the
witness. The synthetic transcript inventory and bounded content are checked
after process and endpoint cleanup. Native diagnostics are compared internally;
the report contains only fixed flags/counts and validated binary identity.

`tests.native_process_capture` supplies bounded nonblocking duplex stdin while
draining both output pipes. `tests.test_native_process_input` covers backpressure,
short writes, interrupted/unready writes, early input closure, timeout, output
overflow and callback cleanup. `tests.test_claude_image_request_contract` exercises
request/history substitutions and the real fake HTTP handler; ordinary canonical
tests run these controls and evidence checks without launching Claude. Explicit
native opt-in is optional; requiring it makes missing binary identity fail.
No real account, remote inference, subscription route, Telegram approval,
production image encoding, local transfer or deployment is proven by this corpus.

