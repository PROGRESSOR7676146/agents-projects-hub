# Owner-step defects

Status: active

Each step below still needs the owner to act and is a defect under
[ADR 0092](../decisions/0092-session-hygiene-and-agent-run-procedures.md).
Opened 2026-11-07; remove an entry only with the change that removes the step.

| Step | Why it still needs the owner | Removal |
|---|---|---|
| Reboot to restart the boot-time Codex app-server | The manual start needs every client quiesced within a 60-second receipt. After an unclean reboot, the first start can also refuse an executing job before workers recover its expired lease; that consumes the one boot attempt | Agent-run start window in the deployment kit; separately design and test boot recovery ordering without replaying work or relaxing custody gates |
| Release the pause after an uncertain provider job | Only the local `indeterminate-resolve` command records the resolution | ADR 0107 Telegram buttons for live Claude jobs, enabled in 0.11.23 pending live acceptance; Codex, OpenCode, Antigravity and recovered jobs keep the command |
| Cancel held requests and start a new session after an owner decision outside Telegram | The installed interface binds held-request cancellation and session reset to Telegram controls, and exposes no supported local administrative procedure for the same explicit owner decision | Provide a bounded local procedure that preserves job history, validates the exact session and ownership, and records the owner's decision without fabricating Telegram callbacks |
| Continue after an unconfirmed final delivery when the notice offers no Telegram decision | A send timeout leaves the final `unknown`; the notice for that case points the owner to the local delivery-hold command in jargon, and later requests in the topic wait until then (one owner request waited 3.5 hours although the answer had arrived) | Offer the existing owner callback whenever the owner is the hold's authority, and say in plain Russian that the previous answer may not have arrived and what each button does |
| Resume maintenance when the initiating agent uses the server being restarted | A reusable independently controlled server-restart operation with exact final-delivery, client drain and rollback evidence is not yet available | Land a reviewed, source-controlled reusable server-restart executor and independent observer with offline fault rehearsal and those proofs; a one-off private adapter does not qualify |

Removed 2026-11-07: the `sudo` runtime install (releases now deploy as user-owned
sealed runtimes, ADR 0089) and cleanup permission (a narrow standing permission
was granted by the owner).
