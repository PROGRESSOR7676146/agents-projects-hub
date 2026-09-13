# ADR 0026: signed GitHub merge privacy boundary

Status: accepted  
Date: 2026-09-13

## Context

The history privacy gate scans commit metadata as well as repository blobs. A
GitHub-hosted pull-request merge normally records the source repository owner in
its first message line and may record the platform account in the author
header. That position can match private-deployment fingerprints even when the
same repository owner is already present in the configured GitHub origin.
GitHub also permits callers to customize the merge title, so this position is
not proven to be generated. Pull-request titles and merge messages remain
user-controlled and must stay under full privacy scanning outside the narrow
exception below.

Text that merely resembles GitHub metadata is not proof of origin. Git can also
select alternate signature formats and verification programs from repository or
user configuration, so a successful configurable `git verify-commit` process is
not an adequate trust boundary.

## Decision

The scanner treats a hosted merge as eligible for a narrow exception only when
all of these conditions hold:

- the real Git headers contain exactly two parents and the exact GitHub
  committer identity;
- the commit has a valid OpenPGP signature from a pinned official GitHub
  web-flow key, verified with fixed system binaries and an isolated keyring;
- the first message line has the strict hosted pull-request merge form; and
- its source owner is a valid GitHub owner name equal to the owner already
  recorded by the local `origin` GitHub URL.

For that case the scanner removes the author header and only the redundant
origin-owner prefix from the first message line. It retains and scans the source
branch, pull-request title and complete body. Unverified commits, fork owners,
invalid owner syntax, alternate verifiers, missing tools, import errors and
timeouts fail closed without an exception. The existing synthetic GitHub
pull-request test-merge rule remains separate.

The official public signing keys are packaged with the scanner so the gate does
not require a network lookup. Key rotation requires an explicit reviewed source
update.

## Consequences

The privacy gate can validate history after a GitHub-hosted merge without a
static allowlist. It has one documented exception for the source-owner position
when that value repeats the configured origin owner; it does not claim that the
position was generated. All other message positions remain fully scanned.
Fork merges whose source owner differs from the public origin owner continue to
fail if that owner matches a protected fingerprint; resolving such a case
requires a separately reviewed policy change.
