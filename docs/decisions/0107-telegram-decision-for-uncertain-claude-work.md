# ADR 0107: Telegram decision for uncertain Claude work

Status: accepted, 2026-11-17 (owner decision, stabilization plan step 3,
after a GPT Astra consult). Source implementation is separate from installed
acceptance. The release that introduces it (0.11.22) ships the capability
off; the owner enabled it in 0.11.23, 2026-11-17, with 0.11.22 as rollback.
Amends: [ADR 0106](0106-quiet-short-russian-notices.md) and REQ-QUEUE-004.

## Context

A Claude turn that ends without a confirmed outcome becomes `indeterminate`
and pauses its project root. Until now the only supported way out was the
local `indeterminate-resolve` command (ADR 0074), which the owner had to run
on the computer: an owner step that ADR 0092 treats as a defect. Resolving an
uncertain job must stay explicit: no automatic replay, an immutable record and
no new work while the old process may still write.

Acknowledging an unknown outcome is not proof that the process stopped. Both
choices can lead to new work in the root, so the offer itself needs that proof.

## Decision

1. **Offer only with proof.** When a Claude worker records an uncertain
   failure from its own live turn, it asks the adapter to prove the turn left
   no process: the process group was killed and the leader reaped, and the
   turn's systemd host scope, when used, is killed again and observed
   inactive. This is the existing stop proof, rechecked at that moment.
   Without the proof, after a restart or recovery, for a streamed-input
   correction pair, or for Codex, OpenCode and Antigravity, the project remains
   paused pending verification.
2. **Prompt.** With the proof, the uncertainty notice gets one more final
   part. The final sender delivers it through the Hub bot as a short prompt
   with **«Проверить и продолжить»** and **«Закрыть без повтора»**. The part
   stores a server-side snapshot of the job, Hub session, generation and
   native session. The notice describes the pause independently of delivery
   of the decision part. Without a Hub bot the part explains that the decision
   is unavailable in Telegram.
3. **Press.** The Controller accepts a press only from a configured owner, on
   a message authored by the configured numeric Hub bot. One immediate
   transaction then rechecks the positively receipted prompt part, its topic,
   the snapshot against the current session (active, Telegram writer, same
   generation and native session), the job (still `indeterminate`, not Codex,
   no resolution, no terminal evidence, no correction pair) and the root.
4. **Record.** The transaction appends the existing `acknowledged`
   resolution and one row in the schema57 `provider_job_owner_decisions`
   ledger: decision, authority `telegram_owner_callback`, actor, callback,
   outbox part, chat, thread, prompt message, session, generation, snapshot
   and continuation job. Triggers refuse updates, deletes and a row without
   its resolution or continuation. A repeated or second press returns the
   decision already taken, without new effects.
5. **Close without retry** releases the root exactly like the local command;
   the queued tail continues in FIFO.
6. **Inspect and continue** additionally requires the root to be free of
   other active or unresolved work, a non-Telegram writer, a running dispatch
   and an unquiesced control sender. It holds the root's queued tail and adds
   one inspection-first job in the same session, with one attempt, recorded as
   a continuation so it runs ahead of the held tail. The held requests get
   the existing held-request buttons.
7. **Switch.** The artifact constant `REMOTE_UNCERTAIN_DECISION` gates the
   offer and the decision; `release-info` reports it. The configuration must
   also have a numeric Hub bot ID and external queue and outbox. The release
   with schema57 ships it off and is the rollback target for the release that
   enables it; enabling is the owner's decision.

`uncertain_decision.py` owns the copy and markers, `uncertain_decision_state.py`
the prompt and decision SQL, `owner_decision_callback.py` the Controller
press handling (with the moved held-request buttons).

## Consequences

- Schema 57 is additive. The 0.11.21 executable cannot open it, so the
  capability ships in two releases: the switch-off release first, then the
  switch-on release deployed with that rollback target.
- The callback grants no replay, approval, stop, writer or delivery authority.
  It cannot resolve a job that the local command already resolved, and the
  command still works after a Telegram decision with the same resolution.
- A changed session, generation or native session makes old buttons refuse.
  Administrative recovery follows the queue recovery runbook.
- The kill-and-reap proof covers the process group and the host scope; like
  the existing stop proof it does not claim isolation from processes that a
  turn deliberately detached outside both.
- Recovered and non-Claude uncertainty use the
  [local recovery procedure](../operations/QUEUE_RECOVERY.md). Extending the
  proof to them is a separate decision.
- Installed and live Telegram acceptance remain pending.
