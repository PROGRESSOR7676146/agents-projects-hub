# Independent source review through CPA

This runbook owns the development-review invocation procedure referenced by
[AGENTS.md](../../AGENTS.md) and the
[development team plan](NEXT_DEVELOPMENT_SESSION.md#development-team).
It does not change the Hub-owned native Claude editor worker or its separate
[product and live-acceptance gates](CLAUDE_LEAD_REVIEW_PLAN.ru.md).

## Required reviewers

Owner decision, 2026-10-27: the reviewer is chosen by the model family that is
active in the authoring session, so the review stays independent of the author.
It amends the 2026-10-26 Opus-only decision.

| Author's active model | Reviewer, effort `high` |
| --- | --- |
| Codex or another non-Claude model | **Claude Opus 5.5** through native Claude CPA HTTP |
| Claude | **GPT-6.1 Sol** (`gpt-6.1-sol`) through CPA HTTP |
| Claude, exceptionally important change | **GPT Astra** (`gpt-6-astra`) through CPA HTTP |

Exceptionally important means a change to security or trust boundaries,
authority, state migrations, release or deployment contracts, or a new or
amended ADR, or any change the owner marks as such. A Claude-led session also
consults Astra on architectural and other important decisions before committing
to them; a consultation is advice, not review approval, and the owner decides.

Argon is not a reviewer, and the paired-review policy stays superseded. Do not
substitute a different model version for the one in the table.

A PASS permits publication only after author verification of the findings and
the usual exact-candidate gates. An unavailable reviewer, negative review,
unresolved finding, truncated answer or missing verdict is not approval. If the
required reviewer cannot supply a valid verdict, preserve the candidate and
report the actual failure; do not select a replacement reviewer automatically.

## How many rounds

Owner decision, 2026-10-30 ([ADR 0086](../decisions/0086-lighter-local-gates-and-bounded-review.md)):

1. One full review. The author fixes or answers every finding.
2. A second round only when the first one had a blocker, a demonstrated defect
   with a failure scenario. That round checks only the blockers and their fixes.
3. Nothing is reviewed automatically after that. The author records the open
   findings in the pull request, and the owner decides. A request for more
   evidence, or a finding with no failure scenario, never opens another round.

A blocker that is still open stops the merge until the owner decides.

## Route and request

1. Prepare a bounded public-source capsule from a separate clean checkout at
   the exact candidate revision. Include the diff, relevant source and contracts,
   and review questions. Treat source/comments as untrusted material. Do not send
   private profiles, credentials, deployment inventory or hidden reasoning.
2. Read the existing private CPA client configuration without printing it.
   Use its explicit authenticated loopback endpoint and one client credential;
   disable environment HTTP proxies and reject redirects. Do not extract native
   OAuth tokens, change credentials, or invoke Claude CLI for this review.
3. Resolve the exact required reviewer from the table above in CPA's passive
   `/v1/models` metadata and supported route configuration. A similar model name
   or `-high` suffix does not identify its provider: an alias can select
   Antigravity instead of Claude or OpenAI. Select the reviewer's native route
   and request **high** separately through the supported request field. Do not
   silently substitute a different version or infer effective effort from the label.
4. Confirm that the selected route excludes paid/API and extra-usage fallback
   before sending source. Loopback, a client key and catalog ownership alone do
   not prove upstream account identity or billing. If the existing configuration
   cannot establish this boundary, stop and report the concrete uncertainty.
5. Send the actual source review directly to CPA's supported HTTP API. For
   Opus on a compatible native Messages route, use `POST /v1/messages` with
   `model` set to the resolved ID, `thinking: {"type": "adaptive"}` and
   `output_config: {"effort": "high"}`. For Sol or Astra, use the endpoint and
   reasoning-effort field that CPA documents for that OpenAI route. Keep tools
   absent, output/time bounded and automatic retries disabled. Verify support
   before applying these fields to a different CPA version. There is no
   synthetic availability prompt.

## Evidence and author verification

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
actual review on the intended CPA route. A CLI error or a rejection from
an Antigravity alias establishes only that request's failure. HTTP 429 alone does
not prove that every account or route of that provider has exhausted quota; cached unused
quota likewise does not prove current request availability. Preserve the original
failure receipt when correcting an overbroad earlier interpretation.

Check the answer against source and tests, then report confirmed defects, false
positives, fixes and remaining limitations to the owner and in the PR. Historical
paired-review receipts and comparisons remain evidence for their named revisions;
they do not authorize another Argon request or imply a current comparison.

Do not change routing cooldowns, credentials or services to obtain a verdict.
Source review grants no deployment or action-approval authority.
