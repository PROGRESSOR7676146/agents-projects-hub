# ADR 0066: Bounded verified Claude image input

Date: 2026-10-10
Status: Accepted source design; publication and live acceptance pending
Owner: Hub maintainer; product decisions remain with the repository owner

## Context

REQ-UX-009 requires content delivery or an explicit material-unavailability
reason. The Claude default reports images unavailable. The pinned native
image/resume corpus establishes a supported structured-stdin path, but a native
success alone is insufficient: malformed images can become text while the CLI
still returns success. Native processing can also change valid image bytes.

## Decision

An explicit local `claude_image_input` boolean defaults to false. It requires
an external Claude worker and rejects combination with protected file tools.
The existing tools-disabled, hook-disabled, strict MCP configuration and CPA
route remain mandatory. No path, URL, Read grant, extra service or role is added.

The worker revalidates existing material/job/project/root/session-generation
bindings. An image snapshot uses descriptor-relative regular-file reads in the
private spool, with size, digest and signature checks. Its immutable bytes are
used for both the project-contained material copy and one new native input.
Preparation does not reopen a verified image path to obtain provider bytes.
PNG/JPEG input is bounded to ten material positions, 2 MiB per image and 4 MiB
aggregate. One NDJSON frame is at most 8 MiB, with a 64 KiB receipt-overhead
reserve. Excess or unsupported materials receive the existing provider-input
and visible-result reason; no input is split or silently truncated.

The caller UUID and exact native session bind `--replay-user-messages` to the
processed user frame. A success requires exactly one current replay receipt,
unchanged prompt/material markers and image positions, and closed inline
base64 PNG/JPEG sources with strict decoding, MIME/signature agreement and
the same image byte budgets. Characterized native PNG-to-JPEG processing and
JPEG re-encoding are allowed; JPEG-to-PNG conversion is not characterized.
This proves correlated processed image blocks, not pixel equivalence,
comprehension, preserved metadata or downstream byte identity. The native
process remains the trusted image-processing boundary. Image-to-text downgrade,
omission, duplicate/stale identity or malformed source cannot create completion.
Native temporary annotations are bounded compatibility data, never authority.

Tools-disabled validation applies to every raw event before receipt filtering.
The processed frame is discarded; ordinary retained output keeps its 2 MiB
limit. Image mode separately bounds one event to 8 MiB, the stream to 12 MiB
and event count to 512. Only the exact correlated completed lifecycle footer
may follow a result. Confirmed typed native failures do not need a success
receipt and retain their existing certainty classification.

The existing adapter owns process registration, nonblocking stdin, concurrent
stdout/stderr drain, stop polling, deadline, group cleanup and completion.
Preparation and reader construction finish before spawn. Incomplete transfer,
missing receipt or protocol failure after spawn retains conservative uncertainty,
materials and root exclusion, without automatic replay or replacement session.

Schema 53 adds a nullable `claude_material_notice` to the existing checkpoint.
The journal atomically saves raw provider completion and the exact Hub-generated
notice, including an empty notice. The notice is checked before invocation at
8,192 characters; it is never truncated only in storage. Completed notice updates
and conflicting journal repeats are refused. Migration preserves legacy NULL;
recovery validates the existing lease/root/session/generation, then appends the
saved notice once. Legacy completion with materials gets a fixed unknown-
availability warning, without reconstructing selection from today's config.
The migration runner owns backup, transaction and rollback; no second state owner
is introduced. Activation requires separately authorized schema-53-compatible
candidate and rollback artifacts, never an older executable on retained schema 53.

## Evidence and next trigger

Offline acceptance covers input/config limits, tamper/symlinks, albums and exact
resume, blocked/short stdin writes, stop/timeout, receipt substitution, owned
worker uncertainty, saved-result recovery, notice idempotency and migration faults.
The pinned native witness independently compares complete requests, processed
receipts and restored history, including distinct transformed maximum-size
PNG/JPEG inputs and corrupt-image false-success controls. It uses fictional data
in disposable namespaces, with no real model, account or Telegram traffic.

Next trigger: exact-source independent reviews and canonical/hosted publication,
then owner-authorized image/caption/album/stop/restart Telegram acceptance.
Human file approvals, authority custody, subscription without paid fallback,
local transfer and saved-session connect remain separate parity gates. Hub
lead/advisor roles remain retired by ADR 0065.

## Closure

The source design is accepted. Publication and deployed acceptance remain open;
the feature is disabled by default and no live migration is authorized here.
