# ADR 0085: Public commit authorship

Status: accepted
Date: 2026-10-30
Owner: repository owner
Supersedes: [ADR 0026](0026-signed-github-merge-privacy-boundary.md)

## Context

The history privacy gate scanned commit metadata with the same rules as files.
Every author, committer or `Co-Authored-By` email outside `example.com` made
history permanently red. To publish the owner's own merges, ADR 0026 added a
narrow exception:

- a private policy file, checked for its mode, owner, links and size;
- pinned GitHub OpenPGP signature verification;
- a GitHub Actions variable that every push compared with the local file
  through the GitHub API.

The owner considers commit authorship public and the rule an obstacle to
ordinary work. Agents could not add the attribution trailers that their tools
add. Each new checkout and the CI needed private setup. Every push depended on
the GitHub API.

## Decision

Commit authorship is publishable. In commit and tag metadata, the email and
private-fingerprint rules do not apply to these spans:

- the identity in the `author`, `committer` and `tagger` headers, including
  those embedded in a `mergetag`;
- the `Name <email>` part of a `Key: Name <email>` trailer line, such as
  `Co-Authored-By` or `Signed-off-by`;
- the source owner in `Merge pull request #N from OWNER/`.

Everything else stays scanned:

- every other rule (tokens, chat IDs, paths, invites, UUIDs, transcript
  markers), including inside those spans;
- the rest of the message, including real project names or emails written in
  prose;
- every file and historical blob.

The following are removed:

- the policy file and its environment variable and Git setting;
- the signature verification and its bundled key;
- the CI preparation step and the repository-variable check in the pre-push
  preflight.

The Claude developer-tools `author_policy` launch key is still accepted, so
existing launch configurations keep working. The scanner no longer reads it.

## Consequences

Commits may carry real author emails and agent attribution trailers. A commit
message can still leak private data through prose, and the gate still catches
that. The pre-push preflight no longer needs network access. History is public
and permanent, so an author who wants privacy should use a GitHub noreply
address.
