# ADR 0102: Images for the Claude host profile

Status: accepted, 2026-11-12 (owner request). Source implementation is separate from installed acceptance.

## Context

Claude images ([ADR 0066](0066-verified-claude-image-input.md)) were accepted only for the tools-disabled
runner, and Hub refused `claude_image_input` together with file-tool profiles
and with streamed corrections (ADR 0083), because each used the same native
stdin channel differently. The owner works with the unconfined `host` profile
(ADR 0095) and has enabled corrections, so photos and screenshots sent to Claude
were refused.

## Decision

1. Under the `host` profile, with `claude_stream_input` enabled, images travel
   in the initial frame of the streamed correction channel. The frame is built
   by the existing bounded encoder (PNG/JPEG, at most ten images, 2 MiB each,
   4 MiB together, 8 MiB per frame) from verified material snapshots.
2. The replay of that initial frame is checked by the existing image receipt:
   text blocks must be unchanged; each image must stay present within the size
   bounds (the CLI may re-encode it); only the native image annotations may
   follow. A correction remains text-only. The reader accepts one event up to
   8 MiB for such a channel.
3. Configuration accepts `claude_image_input` with file permissions or streamed
   input only as `host` plus `claude_stream_input`. Confined profiles keep
   refusing images with tools, and corrections without `host` keep refusing
   images.

A path-in-prompt alternative was rejected: the model would have to choose to
read the file and Hub could not prove the bytes reached Claude.

## Consequences

- Photos and screenshots reach Claude natively in the same turn as the text.
- A replay receipt proves Claude's protocol accepted the image blocks; it does
  not prove the model understood the image.
- The native annotation form under `host` follows the pinned CLI; an
  unrecognized replay fails the turn visibly rather than passing silently.
