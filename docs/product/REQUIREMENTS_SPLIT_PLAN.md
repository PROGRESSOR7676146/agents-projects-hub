# Product requirements split plan

Status: completed with machine-checked content preservation
Date: 2026-09-05

## Objective

Replace the nearly 50 KB product-requirements monolith with a short normative
index and stable capability modules without changing accepted wording,
requirement IDs, lifecycle labels, or link destinations.

The completed split is a documentation migration, not authorization to revise product
behavior. Any later wording change is reviewed separately after the move.

## Baseline and target map

The machine-readable inventory records 88 unique normative requirement IDs and
the SHA-256 of each existing numbered section. The proposed modules preserve
the current section numbers and text:

| Target document | Existing sections | Stable capability |
| --- | --- | --- |
| `PRODUCT_REQUIREMENTS.md` | preamble and 1–5 | normative entry point, mission, scope, terminology, lifecycle |
| `IDENTITY_AND_INTERACTION.md` | 6–9 | project/session identity, routing, Telegram interaction, context, providers |
| `ACCOUNTS_CONTROL_AND_SECURITY.md` | 10–12 | accounts, command surface, writer transfer, approvals and secrets |
| `PERSISTENCE_AND_RECOVERY.md` | 13 | persistence, queues, runtime health and recovery |
| `ONBOARDING_AND_ACCEPTANCE.md` | 14–17 | onboarding, functional/non-functional acceptance, capability matrix |
| `MAINTENANCE.md` | 18–20 | limitations, change policy and provenance |

`requirements_manifest.json` is the stable inventory. The split changed its
`documents` list, but its 88 IDs and 20 section hashes did not.

## Test-first gates

The move was admitted only after these gates passed:

1. The documentation audit must prove every inventoried requirement has exactly
   one definition and no unregistered definition exists.
2. Each numbered section must match its pre-split normalized content hash, so a
   missing paragraph or incidental rewrite fails validation.
3. Every repository Markdown file link and local heading anchor must resolve.
4. The full privacy/history scan must pass with every target module below the
   documentation-size review threshold.

After future structural changes, run the same audit and privacy/history scan
before any commit, followed by the complete canonical validation gate. Review `git diff`
as a move: only the short index/navigation prose and inbound file-level links
may be new; numbered normative section text must remain hash-identical.

## Rollback and stop conditions

The migration is one documentation-only commit after the guardrail commit. A
normal Git revert restores the monolith; no schema, runtime, deployment, or
external state changes are involved.

Stop without splitting if the baseline inventory is not exactly 88 unique IDs
and 20 numbered sections, any section hash changes, any link/anchor is broken,
privacy/history scan reports a finding, or a target file reaches 50 KB. Do not
weaken the privacy threshold, delete an ID, or rewrite normative text to make
the migration pass.
