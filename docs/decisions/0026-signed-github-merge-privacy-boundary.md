# ADR 0026: signed GitHub merge privacy boundary

Status: accepted
Date: 2026-09-13

## Context

The history privacy gate scans commit metadata as well as repository blobs. A
GitHub-hosted pull-request merge normally records the source repository owner in
its first message line and may record an explicitly public contact in the author
email field. That owner position can match private-deployment fingerprints even
when the same repository owner is already present in the configured GitHub
origin.
GitHub also permits callers to customize the merge title, so this position is
not proven to be generated. Pull-request titles and merge messages remain
user-controlled and must stay under full privacy scanning outside the narrow
exception below.

Text that merely resembles GitHub metadata is not proof of origin. Git can also
select alternate signature formats and verification programs from repository or
user configuration, so a successful configurable `git verify-commit` process is
not an adequate trust boundary.

## Decision

The scanner separates permission from provenance. An external declaration
records the owner's permission to publish one exact author email. Repository
policy separately permits the valid owner in the local GitHub `origin` URL when
the same bytes occupy the complete author-name or hosted source-owner field.
Signature verification proves the integrity and supported origin of one merge
object. It does not grant publication permission or authorize an arbitrary
identity.

The declaration path comes from `HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE`. It must be
absolute, outside the scanned checkout, and identify a regular file through a
single opened descriptor. The scanner rejects symlinks, multiple hard links,
non-owner files, modes other than `0600`, files over the bounded size, and
content other than exactly one ASCII email with zero or one terminating LF. It
does not strip, case-fold or normalize the value. Absence or any error gives no
exception and reveals neither the value nor private path in diagnostics. This
file is policy configuration, not a credential or evidence that the invoking
process is trusted. Mode `0600` does not protect it from other code running as
the same user.

The scanner treats hosted and synthetic merges as eligible contexts only when
all common conditions hold:

- the original metadata has one valid tree, exactly two valid parents, one
  structurally valid author, and the exact GitHub committer identity;
- the commit has a valid OpenPGP signature from a pinned official GitHub
  web-flow key, verified with fixed system binaries and an isolated keyring;
- history enumeration, type lookup, content reads and signature verification
  share an isolated no-replacement Git environment, and the bytes read for
  scanning reproduce the requested Git object ID;
- timestamps and timezones are structurally valid; and
- the message has either the strict hosted pull-request form or the strict
  synthetic form `Merge HEAD into BASE` with `BASE` equal to the first parent
  and `HEAD` equal to the second parent.

A hosted merge additionally requires a valid source owner equal to the owner in
the local GitHub `origin` URL. That URL is local configuration used for an exact
comparison; it is not cryptographic proof of repository origin. A synthetic
merge receives no exemption unless its own commit passes the same pinned
signature verification. A similar subject or `GITHUB_ACTIONS` flag is
insufficient.

For an eligible context, the scanner compares absolute byte spans in the
original metadata. It suppresses `non-example email address` and `private
deployment fingerprint` only when a match covers exactly the structural author
email span and those bytes exactly equal the external declaration. It suppresses
only `private deployment fingerprint` on the complete author-name span when its
bytes exactly equal the valid local origin owner. A hosted merge requires those
same bytes to equal its source owner as well; the exact hosted source-owner span
has the same rule-specific exception. Every other rule and position, including
any other display name, committer, branch, title, body and trailers, remains
scanned. Unverified or malformed commits, fork owners, alternate verifiers,
missing tools, import errors and timeouts fail closed without an exception.

Git replacement mappings cannot split the trust decision from the bytes being
scanned: history reads ignore replacements and independently bind each commit,
tag and blob payload to its SHA-1 object ID before applying policy.

The official public signing keys are packaged with the scanner so the gate does
not require a network lookup. Key rotation requires an explicit reviewed source
update.

## Consequences

The privacy gate can validate an authorized public author field without deleting
or pre-filtering surrounding metadata. It has a separate positional exception
for an exact origin owner repeated as the complete author name or hosted source
owner. The local origin remains policy configuration rather than cryptographic
evidence. Tests retain internal match offsets while the external finding format
stays stable.

The reusable Actions workflow transports the repository variable through a step
environment value into a temporary `0600` file outside the checkout, then runs
the single canonical validator. It never interpolates the value into shell
source or stores the file as output, cache or artifact. The variable is not a
secret, is not automatically masked, and is not a security boundary against
pull-request code: candidate code running as the same runner user can read or
modify the file and scanner. Enforcement therefore remains the existing review
of scanner and workflow changes, offline regression tests, and subsequent
verification of the exact accepted SHA; this decision adds no privileged
validation framework.

When the variable is unavailable to a fork pull request or reusable-workflow
invocation, the preparation step sets no declaration and the required canonical
privacy stage still runs. On history that needs the declaration, it reports the
ordinary finding and fails. The workflow uses no `pull_request_target`, write
token, secret, or pull-request title, body or branch input. Fork owners that do
not match local origin and unsigned synthetic merges likewise receive no
exception, which can intentionally keep CI red until policy input and provenance
are both available.
