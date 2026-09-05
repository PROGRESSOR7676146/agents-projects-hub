# ADR 0017: tlive is approval-only for Hub-managed Codex sessions

Status: accepted
Date: 2026-09-05

## Context

Hub and interactive Codex clients intentionally share the optional multi-auth
app-server so both can use account rotation and remote approvals. The tlive
companion subscribes to every loaded thread on that socket. It therefore also
mirrored Hub project prompts and completions into Agent Session Remote and
offered reply-to-continue there.

Approving an action continues the Hub turn already protected by its durable job
and writer lease. Reply-to-continue is different: it starts a new turn directly
through tlive, bypassing Hub routing, queue admission, response delivery, and
the one-writer boundary.

## Decision

Every Hub-owned Codex user turn starts with the exact first-line marker:

```text
TLIVE APPROVAL-ONLY SESSION
```

A compatible tlive companion records that thread as approval-only. It continues
to relay command and file-change approvals, including remote Allow/Deny, but it
does not publish the marked prompt, completion notification, or
reply-to-continue card. Interactive Codex threads without the marker retain the
complete tlive experience.

The marker is transport metadata, not authority. It changes neither Codex's
`workspace-write` sandbox nor its `on-request` approval policy. Hub remains the
only conversation writer for its thread.

## Consequences

- Shared multi-auth rotation and tlive approvals remain available to Hub.
- Project conversation stays in the correct Telegram project group.
- Existing external provider workers, standalone sender, direct-message
  services, mentions, Reply routing, and parallel provider execution remain
  unchanged.
- If tlive restarts after a marked user-message event but before that same turn
  completes, it may not have observed the marker for that in-flight turn. The
  next Hub turn marks the thread again. Avoiding this narrow race would require
  a persistent cross-service registry, which is not justified for notification
  suppression.
