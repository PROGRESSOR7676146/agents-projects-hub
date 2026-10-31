# Claude corrections: compatible runtime rollback

The streamed-input producer 0.10.0 requires the separately retained consumer
0.9.1 as its runtime rollback. That consumer keeps the previous single-input
Claude invocation while consuming retained schema55 work. Rollback preserves
the populated current database; it is not a database restore or an enabled
stream-input release with a renamed wheel.
The owning release gates remain [REQ-OPS-010/011](../product/PERSISTENCE_AND_RECOVERY.md).

Owner: Hub maintainer. The consumer is based on the independently reviewed
schema54 release 0.9.0 and integrates the incident lease fixes. Its distinct
0.9.1 wheel supports schema55; the preceding 0.9.0 executable cannot open it.
The current producer retains exactly the consumer's schema55 DDL and consumers.
Status: source integration; exact publication gates and the artifact-bound
populated-state/configuration-pair drills remain required before use. Publish
the consumer-compatible release first and retain its exact clean wheel before
publishing the producer. Neither artifact can be activated alone without the
distinct compatible pair and the existing release gates. Inspect tracked,
staged and untracked lane state after publication and rollout acceptance before
considering cleanup.

## Retained responsibilities

Schema55 DDL, immutable parent/child links, receipt/result ordering, child
settlement, parent-only stale recovery, context and outbox commits remain the
same consumers in both releases. The retained consumer ships shared value types
and an intentionally unwired input helper; it cannot admit new corrections.
The producer wires that helper only through its default-false local gate.
Raising an old release's schema ceiling alone is insufficient.

In the consumer, `claude_stream_input` must be absent or explicitly false. True
and nonboolean values fail configuration loading. The producer accepts boolean
true only with an owned external Claude worker and without image input.
An immutable rollback configuration must
disable admission explicitly when the active configuration enables it; a
runtime pointer switch alone cannot reconcile incompatible configuration.
Other session/profile/store and native-terminal boundaries remain independent.

An uncertain parent and child each retain their original indeterminate row and
root hold. Releasing the root requires a separate exact owner resolution for
each unresolved job; resolving one must not silently resolve its sibling.

## Required rehearsal

Build both wheels from distinct clean, reviewed revisions. Bind their source
and wheel digests into the private deployment manifest. Run only against
fictional temporary state, without Telegram, provider invocation or service
control. Retain the populated current-schema database during the executable
switch; do not substitute a pre-upgrade backup.

For an enabled producer, retain two immutable configurations and two manifests
using the existing manifest format. The promotion manifest selects producer as
active, consumer as rollback, and the enabled configuration. The rollback
manifest selects consumer as active, producer as rollback, and the explicitly
disabled configuration. Both bind the same exact wheel pair and consistent
backup. Verify each manifest separately and pin both manifest and configuration
digests in the private switch plan. The rollback procedure must select the
consumer runtime, disabled configuration and rollback manifest together. A gate
run against one configuration does not validate the other. Rehearse both
selections with temporary artifacts before any live switch.

The active artifact must populate the fixture; the rollback artifact must
perform the recovery and state checks. Merely importing tests from the rollback
source tree is source coverage, not evidence about the selected wheel pair.
Cover an unreceipted reserved child, receipted child and saved parent completion,
competing recovery claims, covering stop, FIFO tail, and partially receipted
delivery with an unknown next part. Check original links, context, capacity,
outbox identities, send fences and strict receipts, not just schema number.
Every case must prove zero independent child invocation or requeue. Saved
completion settles once; absent completion preserves uncertainty and root hold.

Current focused source coverage is in `tests/test_claude_correction_state.py`,
`tests/test_claude_correction_config.py` and `tests/test_claude_rollback_recovery.py`.
The retained 0.9.1 consumer additionally owns `tests/test_claude_rollback_config.py`.
Passing those tests does not establish installed rollback readiness. Activation
and service actions require their own authorized task and exact manifest gate.
