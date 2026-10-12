# ADR 0053: Evidence before selecting Claude authority custody infrastructure

Status: revised preparation decision; infrastructure selection and acceptance pending.
Date: 2026-10-04.
Owner: Hub maintainer; deployment authorization remains with the repository owner.

## 2026-10-09 scope amendment

[ADR 0065](0065-retire-hub-lead-advisor.md) withdraws the read-only advisor
parity follow-up below. Ordinary helper/launch custody and same-session Claude
local transfer remain open; no infrastructure or security gate is relaxed.

## Context

[REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md) already requires
independently verified OS separation before live file-tool activation.
[ADR 0052](0052-protected-claude-file-permissions.md) confines the native Claude
process, but its worker and tlive hold symmetric keys under the same trusted UID.
An unconfined process sharing that UID can read those keys. Another Linux UID
inside WSL does not establish separation from a process that controls the owning
Windows account and can launch the distribution as root.

The initial preparation selected a dedicated VM before proving that the existing
host needed that migration. This revision supersedes that selection: assess the
smallest enforceable boundary first. It does not accept weaker protection, a new
threat-model exception, a particular UID layout or a deployment. The product's
supported-host policy remains unchanged.

## Decision

Begin with the implemented narrow Claude filesystem/PID boundary and an
inventory of actual agent launch paths, authority files and reachable services.
Determine whether those paths can be excluded from Hub/tlive keys, state and
control endpoints on the existing host. Keep a dedicated Linux VM as a reserve
option only when a concrete unresolved exposure or enforcement cost justifies
it. Neither a VM nor a separate Linux UID is a prerequisite selected by this ADR.

Keep Hub's existing Controller, workers, SQLite, sender and approval transport;
add no queue, database, broker or permission daemon. The native Hub permission
host remains execution/binding owner; the thin pinned tlive extension carries
the human Allow/Deny decision. Reuse the implemented mount pins, immutable
runtime, peer checks and signed receipts. They do not prove host-wide custody.
The current Claude file-tool namespace is the candidate for that particular
path, not a general wrapper for Codex, Hermes, local transfer or advisors.

Assess every model-controlled path that can reach the authority domain,
including development/recovery assistants and helpers outside Hub. Before live
file-tool activation, all such paths must be technically excluded from reading
or changing authority files, controlling trusted processes, invoking host
management or obtaining authority-bearing descriptors from services. Trusted
Hub workers are not themselves model turns. Inventory missing or unsupported
paths as blockers; do not silently remove providers to make the gate pass.

Compare these concrete options without changing the installed topology:

| Option | Evidence required before selection |
| --- | --- |
| Existing host with confined agent launches | Every relevant start/resume, helper, native transfer and recovery path is enforced, with no unconfined alternate path into the authority domain. Host management and exposed services cannot restore that access. |
| Separate Linux execution identities on a trusted host | Actual file, process, endpoint and administrative denial from each agent identity, plus compatible IPC and session/writer handling. Same-account Windows management access must not bypass Linux separation. |
| Dedicated native Linux VM | A demonstrated reason the smaller options cannot meet the gate, and independent control of hypervisor, disks, snapshots, guest root and recovery material. A VM administered by an agent-capable account does not establish that separation. |

Changing the native hook or tlive to a different UID is not a configuration-only
fix: current kernel peer checks require the worker's effective UID. Any changed
IPC boundary needs its own design, tests and review. A separate UID for other
agent paths may preserve that protocol, but still needs installed enforcement
evidence. Flags, mode 0600, signed attestations and cgroup metadata alone prove
none of these options. Do not infer acceptance of an option from this comparison.

Retain immutable root-owned runtime artifacts on supported native Linux
filesystems. Verify that host shares, Windows/WSL interop, control sockets,
forwarding, administrative keys, sudo/polkit grants and recovery access do not
let a model-controlled process regain authority. Backup and recovery custody
have the same separation as live keys.

Networking remains shared in the current Claude namespace for loopback CPA.
Loopback TCP and abstract Unix sockets are therefore reachable. A cooperating
abstract-socket service can transfer a hidden file descriptor with `SCM_RIGHTS`,
making its inode readable despite mount exclusion. Accept only inventoried
service exposure: no authority-revealing or privileged service may admit an
agent, even when it reports the trusted UID. Filesystem mode 0600 and same-UID
peer credentials cannot authenticate that distinction. CPA inference credentials
exposed to Claude must grant no CPA management or host-control capability.
This design does not establish network egress confinement or prevent authorized
project data from being transmitted by a compromised provider.

## Consequences and alternatives

The owner keeps Telegram approvals. Use automated fixtures and actors for
repetitive verification; real permission decisions remain human-owned. Avoid a
platform migration until its security benefit and maintenance cost are concrete.
Existing native sessions and roots cannot be moved or rebound silently.
Same-session Claude local transfer and read-only advisor parity remain open.

A separate WSL distribution alone leaves the Windows-root bypass unresolved.
If an agent can control the owning Windows account's WSL administration, a Linux
UID change alone cannot protect the distribution from that path. Evaluate actual
granted access rather than assuming this holds for every WSL host.
Asymmetric receipt signing can reduce future worker key authority; it cannot
protect a signing host that an agent can control and is not implemented here.
No configuration flag, signed file or cgroup membership read by the same
privileged domain is accepted as proof of management separation.

## Acceptance and next trigger

The [custody preparation runbook](../operations/CLAUDE_CUSTODY.md) owns the
staging, adversarial checks and rollback procedure. The automated rehearsal uses
fictional sentinels and the production namespace builder, not a provider call.
It proves selected kernel behavior and deliberately demonstrates shared-network
exposures. It does not inspect or accept a VM, Windows ACLs, deployed credentials
or subscription routing. Hostile administrators, kernel compromise and
hypervisor vulnerabilities remain outside the stated threat claim.

Next: assess the existing boundary and actual launch/service exposure, identify
the smallest candidate that meets the unchanged gate, and implement only the
missing bounded controls. Prepare exact private deployment artifacts and request
separately scoped authorization when those artifacts are reviewable. Live tools
remain blocked by the existing contract until installed custody and native/human
approval acceptance pass. Recheck this design after launch, service, credential,
runtime, kernel, hypervisor or recovery changes.
