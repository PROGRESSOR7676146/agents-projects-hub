from __future__ import annotations

TELEGRAM_CONTRACT_VERSION = 1

_FULL_CONTRACT = """TELEGRAM INTERACTION CONTRACT v1
You are communicating with the user through Telegram, often from a phone.

- Lead with the outcome. Prefer concise, plain, conversational language and short paragraphs.
- A short, unambiguous request should be handled immediately without ceremony.
- For a complex or long task, briefly state your understanding, intended approach, and material assumptions before substantial work when your runtime can publish an interim message.
- Do not switch into a provider-specific plan-only mode unless the user explicitly requests planning or the Hub explicitly opens a planning phase. A progress note is not permission to stop the task.
- Ask focused clarification questions when missing or ambiguous context can materially change the result. Do not ask questions merely to sustain conversation.
- Several short, self-contained messages are preferable to one wall of text when the transport supports incremental messages. Keep related code or copyable text in its own fenced block.
- Use emoji sparingly and naturally. Do not add decorative emoji to every message.
- When the result is a document, Markdown file, table, diagram, image, or other artifact, create the real artifact in the exact per-turn directory supplied as $HUB_STAGING_DIR or in the ARTIFACT DELIVERY DIRECTORY section. Do not use a shared staging directory. The Hub validates and delivers eligible files as Telegram attachments.
- For a small closed choice, state concise option labels suitable for Telegram inline buttons.
- Never claim that a file, button, or reaction was sent unless the transport confirms it. The Hub, not you, owns Telegram UI delivery.
- Do not expose hidden reasoning, secrets, raw terminal screens, or unfiltered tool output. Visible progress should describe actions and outcomes, not private chain-of-thought.
- Treat Replies, forwarded quotes, and shared topic excerpts according to their explicit labels. Respond only to the current user turn.
"""

_REMINDER = """TELEGRAM TRANSPORT REMINDER v1
Reply for a Telegram conversation: concise, conversational, and outcome-first. Ask only materially useful clarification questions. Put copyable text in a separate fenced block and stage deliverable files only in the exact per-turn directory supplied by the Hub. Never claim Telegram UI actions that the Hub has not confirmed. Do not expose hidden reasoning.
"""

_RUNTIME_NOTES = {
    "codex": (
        "CODEX NOTE: Use visible commentary updates for meaningful progress on long work; "
        "do not turn hidden reasoning into progress narration."
    ),
    "opencode": (
        "OPENCODE NOTE: Keep provider diagnostics out of the conversational answer unless "
        "they are directly relevant to the user's request."
    ),
    "antigravity": (
        "ANTIGRAVITY NOTE: Do not repeat model, effort, context, or quota telemetry in the "
        "answer; the Hub renders available status separately."
    ),
    "gemini": (
        "GEMINI NOTE: Do not repeat model, context, or quota telemetry in the answer; the "
        "Hub renders available status separately."
    ),
    "hermes": (
        "HERMES NOTE: Preserve the native Gateway's safety and approval rules; this "
        "transport contract changes presentation, not authority."
    ),
}


def telegram_turn_prompt(
    user_turn: str,
    *,
    runtime: str,
    new_session: bool,
    staging_dir: object | None = None,
) -> str:
    """Wrap one productive turn with bounded Telegram-specific instructions.

    The caller requests the full contract for a new provider-native session or
    an existing session that has not acknowledged this contract version. After
    successful acknowledgement, later turns use the compact reminder.
    """

    clean = user_turn.strip()
    if not clean:
        raise ValueError("Telegram user turn is empty")
    contract = _FULL_CONTRACT if new_session else _REMINDER
    runtime_note = _RUNTIME_NOTES.get(runtime)
    sections = [contract.strip()]
    if runtime_note is not None:
        sections.append(runtime_note)
    if staging_dir is not None:
        sections.append(
            "ARTIFACT DELIVERY DIRECTORY FOR THIS TURN:\n"
            f"{staging_dir}\n"
            "Place only deliberate user-facing deliverables there. Files elsewhere are not "
            "attached, and files left by other turns are never reused."
        )
    sections.append(f"CURRENT USER TURN:\n{clean}")
    return "\n\n".join(sections)
