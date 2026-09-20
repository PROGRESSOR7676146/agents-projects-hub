# ADR 0034: Fail-fast maintenance validation

Status: accepted
Date: 2026-09-20

## Context and decision

Documentation, release metadata and example configuration were checked after
the entire test suite. A stale requirements digest therefore wasted a full
test run. Move cheap contracts before history scanning, whole-project typing
and tests; preserve every canonical stage and report stage durations. A
separately named focused profile scans tree privacy but omits history, whole-
project typing and test discovery. It supplies feedback without claiming
acceptance; mandatory pre-commit and canonical history scans remain. Commands and
the publication sequence live in the [testing guide](../testing/README.md).

## One final local run, no receipt

Use the existing pre-push canonical run as the sole final local gate on the
clean commit. Focused checks and the mandatory privacy/history scan precede
commit. The hook still validates the exact published refs, current policy and
checkout before and after validation; hosted CI independently runs the full
Python matrix. No hook bypass or CI reduction is introduced.

A receipt cannot reuse the usual pre-commit run under an exact-clean-commit
rule: committing changes the SHA. Moving final validation to the hook already
saves that redundant full run without persisted state. Remaining potential
cache savings apply to repeated pushes of the same commit, such as a network
retry, not the ordinary edit/commit/push cycle.

A sound receipt would additionally need dirty-tree rejection, Python executable
and version/tool environment identity, validator and installed-hook identity,
lockfile and external author-policy invalidation, private atomic storage,
worktree/race handling, and a CI prohibition. Commit identity alone does not
cover mutable local tools, installed hooks or external policy. Supporting and
testing these axes costs more than the remaining repeat-push saving warrants.
Keep the hook fail-closed and uncached. A manual canonical run followed by push
will still repeat; the official sequence deliberately avoids that combination.
Reconsider only if repeated unchanged-commit pushes become a measured bottleneck.

## Documentation ownership and reading

The [maintenance policy](../product/MAINTENANCE.md#19-maintenance-and-change-policy)
assigns one owner to each kind of information. Requirements define behavior;
other documents link instead of repeating it. Section hashes remain strict,
reviewed integrity evidence, not an independent specification. Scoped reading
in AGENTS preserves mandatory security rules and requires the full baseline
for changes to requirements, architecture, security or release/deployment.

## Bounded actor follow-up

Do not extract P0/P1 scenarios in this maintenance change. The roughly 320-line
runner shares ordered message/job observations and one service-restoration
`finally` block; tests patch seven helpers on the actor module. Moving only the
runner would introduce reverse imports or a new callback/context interface,
while moving all helpers expands the review into live acceptance behavior.
A separate mechanical change may isolate shared types/helpers first, retain
the single cleanup boundary, and run the existing seven-scenario and inactive-
service regressions without live execution. No actor behavior changes here.
