# Owner-step defects

Status: active

Each step below still needs the owner to act and is a defect under
[ADR 0092](../decisions/0092-session-hygiene-and-agent-run-procedures.md).
Opened 2026-11-07; remove an entry only with the change that removes the step.

| Step | Why it still needs the owner | Removal |
|---|---|---|
| `sudo` install of a root-owned Claude runtime for each release | The Claude namespace trusts only root-owned runtime code by default | Adopt the user-owned sealed runtime pins of [ADR 0089](../decisions/0089-prepared-release-switcher.md) in the installation, then deploy without `sudo` |
| Removing clean merged worktrees and branches | The agent tool permission layer refuses these deletions | One narrow standing permission granted by the owner |
| Reboot to restart the boot-time Codex app-server | The manual start needs every client quiesced within a 60-second receipt | Agent-run start window in the deployment kit |
