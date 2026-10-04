# ADR 0053: Reference deployment for Claude authority custody

Status: selected preparation design; deployment and acceptance pending.
Date: 2026-10-04.
Owner: Hub maintainer; deployment authorization remains with the repository owner.

## Context

[REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md) already requires
independently verified OS separation before live file-tool activation.
[ADR 0052](0052-protected-claude-file-permissions.md) confines the native Claude
process, but its worker and tlive hold symmetric keys under the same trusted UID.
An unconfined process sharing that UID can read those keys. Another Linux UID
inside WSL does not establish separation from a process that controls the owning
Windows account and can launch the distribution as root.

The owner delegated the infrastructure recommendation. This record selects a
concrete preparation path; it does not create principals, install services,
migrate projects or authorize a deployment. It does not change the product's
supported-host policy or claim that all WSL deployments are unsafe.

## Decision

Prepare a dedicated native Linux VM on a native Linux filesystem, with its
hypervisor administration, guest root and recovery material controlled by an
administrative identity that never runs productive agents. Every agent domain,
including development/recovery assistants outside the VM, must be excluded from
that management identity. A VM owned by the same agent-capable Windows account
does not meet this design. Guest root isolation alone is insufficient if an
agent can read virtual disks, restore snapshots or operate the hypervisor.

Keep Hub's existing Controller, workers, SQLite, sender and approval transport;
add no queue, database, broker or permission daemon. Inside the guest, trusted
non-root Hub/tlive processes may retain their current UID and peer protocol.
All model-controlled execution under that UID must be confined before execution,
with no unconfined alternative launch. The current Claude file-tool namespace is
the implemented candidate for that particular path. It is not a general wrapper
for Codex, Hermes, other providers, local transfer or advisors.

Other agent processes must use independently enforced nonprivileged domains
that cannot read or change authority files, ptrace trusted processes, invoke
guest/host management, or obtain descriptors from trusted services. Trusted Hub
workers are not themselves model turns. A separate provider UID can host a
Codex app-server only after its socket, project/session access, approvals and
launch paths have been designed and accepted; this is not implemented by this
record. Until every required provider path has such a boundary, a complete Hub
cutover is blocked. Do not silently remove providers to make it pass.

Use immutable root-owned runtime artifacts. Exclude host shares, Windows/WSL
interop, guest-control sockets, desktop/agent forwarding, administrative keys,
sudo/polkit grants and recovery mounts from all productive domains. Backup and
recovery custody have the same separation as live keys.

Networking remains shared in the current Claude namespace for loopback CPA.
Loopback TCP and abstract Unix sockets are therefore reachable. A cooperating
abstract-socket service can transfer a hidden file descriptor with `SCM_RIGHTS`,
making its inode readable despite mount exclusion. Deploy only an inventoried
minimal service set: no authority-revealing or privileged service may admit an
agent, even when it reports the trusted UID. Filesystem mode 0600 and same-UID
peer credentials cannot authenticate that distinction. CPA inference credentials
exposed to Claude must grant no CPA management or host-control capability.
This design does not establish network egress confinement or prevent authorized
project data from being transmitted by a compromised provider.

## Consequences and alternatives

The owner keeps Telegram approvals; routine work does not require manual test
clicks. Installation, immutable updates, project placement, administrative
access, backup and restore gain an explicit OS boundary and corresponding
maintenance. Existing native sessions and roots cannot be moved or rebound
silently. Same-session local transfer and read-only advisor parity remain open.

Separate UIDs on a trusted native Linux host remain a possible alternative.
A separate WSL distribution alone leaves the Windows-root bypass unresolved.
Using a VM under an agent-controlled administrator also leaves custody unresolved.
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

Next: implement and verify the missing provider-domain launch boundaries,
prepare exact private deployment artifacts, and request separately scoped
deployment authorization only when those artifacts are reviewable. Live tools
remain blocked by the existing contract until installed custody and native/human
approval acceptance pass. Recheck this design after launch, service, credential,
runtime, kernel, hypervisor or recovery changes.
