# Release metadata synchronization

Status: accepted policy  
Last updated: 2026-09-05

This policy keeps reusable release claims consistent without treating a version
or Git tag as deployment evidence.

## Sources and invariants

- `pyproject.toml` is the package-version source and uses canonical `X.Y.Z`
  SemVer while the public API is evolving.
- `docs/status/PROJECT_STATUS.md` names the same version as `Release: vX.Y.Z`.
- `CHANGELOG.md` begins with `[Unreleased]`; its newest released entry equals
  the package version. Every existing `vX.Y.Z` tag has a matching changelog
  entry.
- A release tag pointing at the checked-out commit must equal the package
  version. A tag describes that release commit; it does not establish which
  executable is running.
- The exact clean Git SHA embedded in an immutable artifact and reported by all
  required runtime components remains the authoritative deployment identity.

`python -m hermes_codex_router.release_metadata .` checks these rules. Metadata
contradictions fail the command. Missing tags are reported in `debts` but do not
fail an ordinary branch build: a repository check must never create a tag or
force an unauthorized history mutation. The canonical validation script and CI
run this audit.

## Release sequence

1. Move the intended entries out of `[Unreleased]` into one dated version
   section.
2. Update the package version and project-status release in the same commit.
3. Run the canonical validation gate and build from an exact clean commit.
4. With separate owner authorization, create `vX.Y.Z` only on that validated
   commit and push it without moving or rewriting an existing tag.
5. The tag workflow verifies the tag/package match before creating a GitHub
   release. Promotion then uses the immutable artifact and records its exact SHA
   in private deployment evidence.

If a tag is missing, leave the debt visible until tag creation is explicitly
authorized. If an existing tag is wrong, stop: do not retarget or delete it as
an automatic repair. Publish a corrective version through an owner-reviewed
decision instead.
