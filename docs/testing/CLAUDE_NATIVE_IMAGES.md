# Offline native Claude image continuation

`tests.test_claude_image_session` is a separate test-only compatibility corpus
for the explicitly pinned CLI 2.1.285. The production image gate defaults to
false; this prerequisite does not enable it. Run it manually with a locally
verified standalone ELF:

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
MCP, plugins and skills. The real stream reader/parser must validate both expected
visible results under the chosen UUID and model.

Each invocation has its own bounded endpoint, fully closed with handlers joined
before the next process starts. Positive invocations allow one Messages POST and
one optional strictly validated passive HEAD. A third process requests a known
nonexistent UUID: it may issue only that passive HEAD, must write all stdin bytes and
exit naturally, and must return one strict structured missing-session failure.
Malformed, duplicate or conflicting terminal data, success under another UUID,
any attempted POST, unknown request, timeout or transcript mutation fails the
witness. The synthetic transcript inventory and bounded content are checked
after process and endpoint cleanup. Native diagnostics are compared internally;
the report contains only fixed flags/counts and validated binary identity.
Complete writes prove pipe acceptance; the positive request oracle additionally
proves native consumption. The missing-session check does not prove consumption
and makes no claim that the supplied image was processed. Exit 1 and one HEAD
were observed; the evidence contract permits a natural exit code in 0–255 and
zero or one validated HEAD.

`tests.native_process_capture` supplies bounded nonblocking duplex stdin while
draining both output pipes. `tests.test_native_process_input` covers backpressure,
short writes, interrupted/unready writes, early input closure, timeout, output
overflow and callback cleanup. `tests.test_claude_image_request_contract` exercises
request/history substitutions and the real fake HTTP handler; ordinary canonical
tests run these controls and evidence checks without launching Claude. Explicit
native opt-in is optional; requiring it makes missing binary identity fail.
No real account, remote inference, subscription route, Telegram approval,
production image encoding, local transfer or deployment is proven by this corpus.

## Owned production input witness

The separate `tests.test_claude_worker_image_native` witness uses the production
encoder and owned process/reader under the same disposable native boundary:

```bash
HUB_NATIVE_CLAUDE_FIXTURE_EXECUTABLE=/home/example/tools/claude-native \
  HUB_NATIVE_CLAUDE_FIXTURE_SHA256='<locally-verified-sha256>' \
  HUB_NATIVE_CLAUDE_FIXTURE_VERSION='2.1.285 (Claude Code)' \
  HUB_REQUIRE_NATIVE_CLAUDE_WORKER_IMAGE_TESTS=1 \
  PYTHONPATH=src:. python -m unittest -v tests.test_claude_worker_image_native
```

It retains the complete independent request/history oracle and compares the
actual correlated replay frames too. Fresh/resume inputs include distinct PNG
and JPEG padded to 2 MiB each, with swapped image order in the resumed turn;
their native processed JPEG outputs are fixed fictional fixture bytes. The
original small corpus additionally covers unchanged images. Signature-only PNG,
JPEG and a mixed valid/corrupt album deliberately receive endpoint success after
native image-to-text downgrade. The unguarded reference must demonstrate that
false success; the production guard must refuse it. A success marker or complete
stdin write cannot substitute for the processed receipt. Receipt data is never
forwarded as visible output.

Ordinary tests cover limits/closed sources, short/blocked writes, stop/timeout
during input and incomplete acknowledgement, public-worker missing/downgraded
receipts, retained materials/root and no replay. Schema-53 tests cover raw
completion plus notice atomicity, delivery-preparation faults, stale recovery
after configuration change, legacy unknown availability, backup and migration
rollback. These gates do not prove full deployed worker/Telegram E2E, real image
comprehension, human approvals, subscription routing or authority-data custody.
