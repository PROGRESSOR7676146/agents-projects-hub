# Offline native Claude model and effort continuation

The test-only `tests.test_claude_native_selection` corpus pins CLI 2.1.285 and
three independently selected text scenarios. It extends the existing
[native transport boundary](README.md#optional-offline-native-claude-transport),
without changing the runtime, configured catalog, permissions or session state.

| Scenario | Fresh invocation | Exact resume |
| --- | --- | --- |
| Forward | `claude-opus-5-5`, high | `claude-sonnet-4-6`, medium |
| Reverse | `claude-sonnet-4-6`, medium | `claude-opus-5-5`, high |
| Effort only | `claude-opus-5-5`, high | `claude-opus-5-5`, medium |

```bash
HUB_NATIVE_CLAUDE_FIXTURE_EXECUTABLE=/home/example/tools/claude-native \
  HUB_NATIVE_CLAUDE_FIXTURE_SHA256='<locally-verified-sha256>' \
  HUB_NATIVE_CLAUDE_FIXTURE_VERSION='2.1.285 (Claude Code)' \
  HUB_REQUIRE_NATIVE_CLAUDE_SELECTION_TESTS=1 \
  PYTHONPATH=src:. python -m unittest -v tests.test_claude_native_selection
```

Each scenario uses a disposable HOME and root in a network/PID/IPC namespace,
dummy API-key authentication and two sequential native processes. Both argv
arrays are constructed separately by the production text builder. The fixture
adds empty MCP/settings sources, a replacement system prompt, one-turn limit,
disabled model switching/fallback, retry suppression and a token bound. It
retains session persistence inside that disposable namespace. These additions
are fixture bounds, not proof of production retry or billing behavior.

The host selects a finite case before invocation. The endpoint requires exact
current model/effort, UUID in both headers and metadata, previous user and
assistant text, new prompt, model-specific scaffolds, cache placement and beta
headers. It strictly parses original JSON and raw header pairs before projecting
only already-validated selection fields into the original complete request
oracle. All other native fields retain that oracle. Expectations are not
constructed from the arriving request or inferred from argv. Each endpoint
consumes its single POST even when invalid; only a validated request receives
the independently prepared model and visible marker.

This CLI reencodes historical scaffolds when changing model: forward converts
the old environment into user reminders and removes historical effort; reverse
reconstructs the old Sonnet environment with current high effort. Effort-only
resume retains historical high and inserts current medium. Those exact shapes
are pinned compatibility data. The witness proves exact user/assistant dialogue
continuity and current emitted settings, not preservation of every historical
service field or effective reasoning effort.

The real reader/parser validates empty init capabilities, exact result model,
UUID and one visible response per phase. After process cleanup, every handler
is drained before advancing. Per-phase evidence requires one POST, validation
and served response; totals cannot conceal a missing phase or duplicate call.
Exactly one bounded synthetic session file must exist. A third production-built
invocation resumes a nonexistent UUID: explicit structured refusal, zero POST,
no replacement session and unchanged original transcript are mandatory.

Ordinary canonical tests run synthetic positive and substitution controls through
the actual HTTP handler and verify evidence refusal and all production argv
flags/settings. They cover changed/deleted/reordered/duplicated history, current
and historical settings, UUID, strict JSON, raw headers and consumed attempts.
Explicit native opt-in skips when absent; required mode fails when identity or
isolation is unavailable. No live inference, entitlement, actual model
availability, subscription/CPA route, paid-fallback exclusion, Telegram E2E,
human approval, local transfer, saved-session discovery or deployment is proven.
