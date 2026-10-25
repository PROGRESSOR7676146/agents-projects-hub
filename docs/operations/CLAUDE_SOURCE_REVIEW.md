# Independent source review with Opus and Argon

This runbook owns the development-review invocation procedure referenced by
[AGENTS.md](../../AGENTS.md) and the
[development team plan](NEXT_DEVELOPMENT_SESSION.md#development-team).
It does not change the Hub-owned native Claude editor worker or its separate
[product and live-acceptance gates](CLAUDE_LEAD_REVIEW_PLAN.ru.md).

## Required reviewers

Send each review independently to **Claude Opus 5.5, effort high**, through the
native Claude CPA HTTP route, and **agy Gemini Argon High**, using the exact
`gemini-4-argon-high` catalog variant. Both reviewers receive the same
bounded source capsule, candidate revision and questions, without seeing the
other reviewer's answer. Do not silently substitute models. Authorization to
replace Opus 5.5 with Opus 4.8 is revoked; Argon is a second reviewer, not merely
a fallback. If either route is unavailable, retain its failure evidence and the
other review separately; report incomplete paired review rather than inventing
an answer or approval. Owner review remains available under AGENTS.md.

## Route and request

1. Prepare a bounded public-source capsule from a separate clean checkout at
   the exact candidate revision. Include the diff, relevant source and contracts,
   and review questions. Treat source/comments as untrusted material. Do not send
   private profiles, credentials, deployment inventory or hidden reasoning.
2. Read the existing private CPA client configuration without printing it.
   Use its explicit authenticated loopback endpoint and one client credential;
   disable environment HTTP proxies and reject redirects. Do not extract native
   OAuth tokens, change credentials, or invoke Claude CLI for this review.
3. Resolve the exact owner-selected **Claude Opus 5.5** in CPA's passive
   `/v1/models` metadata and supported route configuration. A similar model name
   or `-high` suffix does not identify its provider: an alias can select
   Antigravity instead of Claude. Select the native Claude route and request
   **high** separately through the supported request field. Do not silently
   substitute a different Opus version or infer effective effort from the label.
4. Confirm that the selected route excludes paid/API and extra-usage fallback
   before sending source. Loopback, a client key and catalog ownership alone do
   not prove upstream account identity or billing. If the existing configuration
   cannot establish this boundary, stop and report the concrete uncertainty.
5. Send the actual source review directly to CPA's supported HTTP API. For a
   compatible native Messages route, use `POST /v1/messages` with `model` set to
   the resolved ID, `thinking: {"type": "adaptive"}` and
   `output_config: {"effort": "high"}`. Keep tools absent, output/time bounded
   and automatic retries disabled. Verify support before applying these fields
   to a different CPA version. There is no synthetic availability prompt.
6. Resolve Argon from the passive `agy models` catalog. Invoke agy with the
   exact High variant above; do not add a separate `--effort` flag when this
   model does not support it. If that ID is absent, report unavailable rather
   than choosing a similar model. Use a separate working directory containing
   only the public capsule, with no private state or credentials in its scope.
   Use read-only plan/sandbox mode, with bounded
   output/time and no editing, delegation, live probes or deployment. Retain the
   selected model and invocation receipt; distinguish requested selection from
   independently reported actual model/effort. A completed helper job record is
   not an answer: retrieve and inspect its visible result.

## Evidence and comparison

Retain a private receipt binding the clean candidate, capsule digest, requested
model/effort, transport, returned model, completion status and visible findings.
Discard thinking blocks; never forward hidden reasoning. Record exact revision,
actual reviewer, findings/fixes and evidence limits in the PR. A model reviewing
supplied source has not executed tests; record author-run checks separately.
An incomplete response is not approval. Exhausting the output budget without a
visible verdict is not quota exhaustion: preserve that receipt and deliberately
adjust the bounded review if needed. Requested effort is not proof of effective
effort unless supported response evidence establishes it.

Determine availability from supported passive metadata or a failure of the
actual review on the intended CPA Claude route. A CLI error or a rejection from
an Antigravity alias establishes only that request's failure. HTTP 429 alone does
not prove that every Claude account or route has exhausted quota; cached unused
quota likewise does not prove current request availability. Preserve the original
failure receipt when correcting an overbroad earlier interpretation.

After each paired review, check both sets of findings against the source and
available tests. Tell the owner who performed better on this particular review
and why: confirmed defects, missed issues, false positives and actionable
recommendations matter more than answer length or confidence. A tie is valid;
if a response is missing or the evidence cannot separate them, say that the
comparison is inconclusive. Do not infer general model superiority from one
review. Keep the actual reviewer/model, findings, author verification and this
comparison in the PR; deployment-specific evidence stays private.

Do not change routing cooldowns, credentials or services to obtain a verdict.
If both review paths fail, retain a review-ready candidate for owner review.
Neither review nor comparison grants deployment or approval authority.
