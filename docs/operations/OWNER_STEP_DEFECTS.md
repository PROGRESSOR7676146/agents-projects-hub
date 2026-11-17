# Owner-step defects

Status: active

Each step below still needs the owner to act and is a defect under
[ADR 0092](../decisions/0092-session-hygiene-and-agent-run-procedures.md).
Opened 2026-11-07; remove an entry only with the change that removes the step.

| Step | Why it still needs the owner | Removal |
|---|---|---|
| Reboot to restart the boot-time Codex app-server | The manual start needs every client quiesced within a 60-second receipt | Agent-run start window in the deployment kit |
| Release the pause after an uncertain provider job | Only the local `indeterminate-resolve` command records the resolution | ADR 0107 Telegram buttons for live Claude jobs, enabled in 0.11.23 pending live acceptance; Codex, OpenCode, Antigravity and recovered jobs keep the command |
| Resume maintenance when the initiating agent uses the server being restarted | A reusable independently controlled server-restart operation with exact final-delivery, client drain and rollback evidence is not yet available | Land a reviewed, source-controlled reusable server-restart executor and independent observer with offline fault rehearsal and those proofs; a one-off private adapter does not qualify |

Removed 2026-11-07: the `sudo` runtime install (releases now deploy as user-owned
sealed runtimes, ADR 0089) and cleanup permission (a narrow standing permission
was granted by the owner).
