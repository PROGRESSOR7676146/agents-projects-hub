# Claude corrections: compatible runtime rollback

This source variant keeps the previous single-input Claude invocation while
consuming retained schema55 work. It is a separate compatibility release, not
a database restore or an enabled stream-input release with a renamed wheel.
The owning release gates remain [REQ-OPS-010/011](../product/PERSISTENCE_AND_RECOVERY.md).

Owner: Hub maintainer. Base: the independently reviewed schema54 release 0.9.0.
This compatibility release is 0.9.1, with a distinct wheel identity and schema55
support; the preceding 0.9.0 executable remains incompatible with schema55.
Status: source preparation; exact combined-source review, canonical validation
and an artifact-bound populated-state drill remain required before use.
The incident lease fixes are integrated. The next trigger is exact-source
review and publication, followed by the artifact-pair rehearsal with the
reviewed producer candidate. Publish this consumer-compatible release before the stream-input
producer release; retain its exact clean wheel as the rollback artifact. Then
integrate and review the producer against that base. Inspect tracked, staged and untracked lane state after publication
and rollout acceptance before considering cleanup.

## Retained responsibilities

Schema55 DDL, immutable parent/child links, receipt/result ordering, child
settlement, parent-only stale recovery, context and outbox commits remain the
same consumers as the stream-input candidate. `claude_corrections.py` includes
value types needed by those consumers; the runtime does not wire its input
producer into invocation. The shared producer helper is deliberately inert in
this release; it does not enable input admission. Raising an old release's schema ceiling alone is
insufficient.

`claude_stream_input` must be absent or explicitly false. True and nonboolean
values fail configuration loading. An immutable rollback configuration must
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
`tests/test_claude_rollback_config.py` and `tests/test_claude_rollback_recovery.py`.
Passing those tests does not establish installed rollback readiness. Activation
and service actions require their own authorized task and exact manifest gate.
