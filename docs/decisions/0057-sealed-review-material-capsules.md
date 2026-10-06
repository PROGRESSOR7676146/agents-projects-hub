# ADR 0057: Sealed explicitly selected review materials

Status: offline implementation candidate; publication review and workflow integration pending.
Date: 2026-10-07.
Owner: Hub maintainer.

## Context

The [neutral namespace core](0056-provider-neutral-process-namespace.md) can
enforce readonly project mounts. Mount pinning does not freeze directory contents,
and mounting an entire project exposes more than a bounded authorized review.
Existing digest-checked spools are same-UID files, not kernel-sealed snapshots.
The [initial advisor contract](../product/ACCOUNTS_CONTROL_AND_SECURITY.md)
therefore needs an explicit material boundary before productive integration.

## Decision

Add the dependency-neutral `review_materials.py` primitive. A trusted caller
supplies an already authorized canonical root, explicit relative file names,
expected byte counts and SHA-256 values, and a bounded opaque result binding.
The primitive grants no authorization, discovers no files, and accepts no chat
paths. Future durable workflow authorization remains a separate transaction.

Pin no-follow sources, accept only regular single-link files on the supported
root filesystem/mount, and recheck names and source metadata before completion.
Refuse Git metadata, duplicates, escapes, symlinks, special files, unsupported
mounts and changed/digest-mismatched input. Selected content must be UTF-8 text
without NUL; project configuration, credentials and native transcripts are
never included implicitly. The caller is responsible for authorizing content,
including any sensitive text: filename checks are not a secret detector.

Capture at most 32 files, 64 KiB each and 256 KiB total source bytes. Canonical
JSON contains one opaque binding and the sorted name/size/digest/text entries,
with a 1 MiB encoded limit. Strict decoding requires its exact capsule digest,
schema, content digests, bounds and ordering; duplicate keys and corrupt or
noncanonical input refuse. The capsule digest is the exact artifact reference;
no Git revision or whole-project snapshot is attested.

Own a close-on-exec Linux memfd sealed against writes, growth, shrinkage and seal
changes. Use the Python API when available; Python builds omitting that wrapper
use the named libc `memfd_create` API and Linux UAPI seal constants. Unsupported
kernel/libc support fails closed, with no filesystem or unsealed fallback.
The context manager owns close and every failed construction closes descriptors.
Deliver bounded verified bytes through the caller-owned stdin channel, not the
host memfd. The primitive has no subprocess invocation or provider interface.

An offline actor receives selected text inside the existing private namespace,
using a disposable empty project skeleton and a separate session HOME. The
original project and its Git are not mounted. Parent/exec child denial and FD
checks establish only the fixture boundaries. Required strict CI includes this
witness; the [testing guide](../testing/README.md#live-acceptance-boundary) owns
check commands and evidence limits.

## Consequences and next trigger

Captured bytes remain immutable after source changes. Source pinning and metadata
checks are conservative point-in-time checks, not exclusion of every hostile
host actor. A process crash loses this ephemeral capsule; it does not authorize
productive replay. There is no queue schema, role workflow, provider transport,
approval, deployment or installed custody change.

Next bind the digest to the accepted role/session/result selection at the
existing workflow boundary and recheck it before invocation. Independently
establish the supported native request surface and a bounded isolated inference
transport. One review round does not imply one HTTP request; undocumented CLI
Unix-socket or inherited-FD inference must not be assumed. Subscription/no-paid
fallback, safe role transfer, local/helpers/MCP custody, independent review and
live acceptance remain open under the existing contracts.
