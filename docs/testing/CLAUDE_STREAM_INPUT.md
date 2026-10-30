# Native Claude streamed-input characterization

This is an offline protocol fixture, not Hub steering implementation or live
Telegram acceptance. Owning behavior remains
[REQ-ROUTE-010](../product/IDENTITY_AND_INTERACTION.md#message-semantics).

## Reproduce

Supply the standalone native Claude 2.1.296 binary and its exact SHA-256:

The host also needs `/usr/bin/bwrap`, permitted unprivileged user/PID/network
namespaces and `/usr/bin/python3` at least 3.10 inside the fixture. The ordinary
source suite requires Python 3.11 or later. Ubuntu AppArmor may deny namespace
creation; an unavailable prerequisite is a failed required native run, not evidence
about the CLI or Hub. Developer environments without the explicit opt-in skip it.

```bash
HUB_NATIVE_CLAUDE_FIXTURE_EXECUTABLE=/home/example/tools/claude \
HUB_NATIVE_CLAUDE_FIXTURE_SHA256=EXACT_BINARY_SHA256 \
HUB_NATIVE_CLAUDE_FIXTURE_VERSION='2.1.296 (Claude Code)' \
HUB_REQUIRE_NATIVE_CLAUDE_STREAM_INPUT_TESTS=1 \
PYTHONPATH=src:tests python -m unittest -v tests.test_claude_native_stream_input
```

The fixture copies and pins the supplied binary, then uses an empty filesystem,
network and PID namespace. It exposes only system runtime, a disposable project
and fictional local Messages server. Real credentials, provider routes, Hub
state, Telegram and installed services are absent. The server permits two model
requests per scenario, with retries disabled; there is no remote inference.

Both cases send the correction after the first model request reaches the server
and before its scripted response is released. The first case returns a Read
tool call; the second returns a final text answer. Stdin remains open until the
expected one or two terminal records have actually been read; only then does
the fixture send EOF. Writing the correction does not synchronize native
consumption: its position is established by the resulting receipt/request trace.

| Native scenario | Required observations |
| --- | --- |
| Tool loop | Second request includes original input, correction and successful Read content; correction echo precedes the only terminal result |
| Final answer | Two native terminal results occur in the same session; the first precedes the correction echo and request two includes the correction |

Both require exact ordered input UUIDs/content, matching session, successful
process/results, request bounds (including unsupported methods and parser errors)
and independent host-file/loopback exclusion.
An echo, successful pipe write or exit code alone does not prove consumption
within the original turn. The fixture does not test injection during a running
Bash command, corrections sent after the first result, idle behavior with an
indefinitely open pipe, native interruption, permissions, model comprehension or billing.

## Consequence for the next source increment

`--input-format stream-json --replay-user-messages` is a usable native input
channel. It is not an expected-active-turn precondition like Codex `turn/steer`.
The current Hub runner closes stdin and accepts one terminal result; merely
leaving its pipe open would neither preserve durable child ownership nor support
the observed second terminal result.

Before enabling it, bind every admitted correction durably before writing,
verify its exact processed-input receipt and distinguish consumption before
the original result from a subsequent native turn. Preserve pending messages,
same-root exclusion and uncertainty through stop, EOF, restart and failed
receipt/result persistence. Do not automatically interrupt tools or interpret
an echoed input as completed work. Late input must retain FIFO semantics;
unknown submission must not be replayed. Keep existing profiles and extensions
unchanged. Source review and separately authorized live acceptance remain open.

References: [official streaming input](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode.md),
[CLI replay flag](https://code.claude.com/docs/en/cli-reference.md#cli-flags),
[interruption and buffered results](https://code.claude.com/docs/en/agent-sdk/python.md#claudesdkclient).
