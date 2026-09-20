# ADR 0032: Durable inbound Telegram materials

- Status: Accepted
- Date: 2026-09-20

## Context

Telegram text admission was durable, but a caption-only document disappeared
before routing and attachment metadata never proved that a provider received
the bytes. Media groups could spend one provider turn per part, while a file
arriving during active Codex execution could be mistaken for text-safe steering.
Forwarded files also had to remain passive data rather than become routing or
approval authority.

The hosted Bot API `getFile` contract allows downloads only up to 20 MB and
guarantees a file link for at least one hour. Telegram Premium changes
user-account upload capabilities, not this bot API download contract. A cloud
bot that cannot obtain the bytes cannot split them locally. The official local
Bot API server can download without a size limit, but adopting it is a separate
deployment and trust-boundary decision.

## Decision

Schema 33 adds one nullable album/group key to `provider_jobs` and one
`incoming_materials` table. Each material stores its numeric Telegram receipt,
topic/project/execution scope, target provider session generation, origin,
content class, private path, size, digest, availability state and bounded
failure reason. Job input and material rows commit in one SQLite transaction;
the unique Telegram receipt makes restart and duplicate updates idempotent.
Migrations 1–32 remain byte-for-byte unchanged.

The credential-owning Controller downloads only Telegram `file_id` values into
a private deterministic raw spool. It never follows user URLs or filenames.
Admission accepts UTF-8 text and signature-verified JPEG, PNG, GIF or WebP;
archives and unsupported document/media types are not opened. Limits are 20
MiB per file, ten parts and 80 MiB per job. A media group waits for a bounded
two-second quiet window and creates one provider job. A file-only message gets
a neutral productive instruction. Before downloading a later album part, the
Controller atomically holds only the matching unleased tail job. The hold is
bounded by the Bot API download timeout and by the ten-part absolute collection
window; committing each part restores the ordinary two-second quiet window.
Already leased or executing work is never reclaimed, so a genuinely late part
remains FIFO instead of mutating an active provider turn.

Workers receive no Telegram token. After the existing root/lane check they
revalidate the private canonical path, reject symlinks, verify size/digest and
copy the snapshot to `.hub/incoming/<job_id>` under the exact execution root.
UTF-8 document content is placed in the actual provider input, with a verified
relative copy for large bundles. Codex images use native app-server
`localImage`. OpenCode and Antigravity images are reported unavailable until a
native image-input contract is separately accepted. Every material block is
explicitly lower-priority user data and cannot select a root, route, writer,
sandbox or approval.

Material received after a turn starts is a later FIFO job and is excluded from
same-turn steering. A forward downloads and stores material without starting a
provider; only the next productive turn in the same bound session generation
can attach it. Session/provider/root changes therefore cannot silently inherit
the old material. Legacy inline routes reject attachments visibly instead of
pretending metadata was content.

Successful provider-result commit atomically marks stored rows consumed;
best-effort cleanup then removes raw and execution-root copies. Pre-execution
integrity failure terminates visibly without invoking the provider. A stale
pre-execution lease can reuse the verified snapshot, while ambiguous execution
retains raw evidence and remains non-replayable under the existing queue rule.

## Consequences

- Caption, document, selected-quote, album, passive-forward and late-input
  markers are asserted against the actual fake-provider call, not a prompt
  builder in isolation.
- Unsupported or oversized input is durable and visible to both provider and
  Telegram user; a claimed filename is never treated as proof of access.
- Rollout and runtime rollback artifacts must both support schema 33.
- Premium and an MTProto acceptance account are not production file proxies.
  A future local Bot API rollout needs its own configuration, security review,
  backup, canary and live acceptance.
- Repository validation proves implementation only. It does not prove a live
  bot, provider or installed runtime received a real Telegram file.
