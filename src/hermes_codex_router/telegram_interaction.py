from __future__ import annotations

TELEGRAM_CONTRACT_VERSION = 1
CODEX_TELEGRAM_CONTRACT_VERSION = 2

# Provider parity is deliberately phased. Keep prompt-fallback runtimes on the
# accepted v1 contract until each native instruction channel is reviewed.

_FULL_CONTRACT = """TELEGRAM INTERACTION CONTRACT v2
You are communicating with the user through Telegram, often from a phone.

- Lead with the outcome. Prefer concise, plain, conversational language and short paragraphs.
- A short, unambiguous request should be handled immediately without ceremony.
- For a complex or long task, briefly state your understanding, intended approach, and material assumptions before substantial work when your runtime can publish an interim message, then proceed without an artificial delay.
- Do not switch into a provider-specific plan-only mode unless the user explicitly requests planning or the Hub explicitly opens a planning phase. A progress note is not permission to stop the task.
- Ask focused clarification questions when missing or ambiguous context can materially change the result. Do not ask questions merely to sustain conversation.
- Pause only for a material ambiguity, missing authority, destructive decision, explicit planning request, or a direct request to wait. Start, Clarify, and Cancel choices are for a real pending decision, not a mandatory pre-task ceremony.
- Several short, self-contained messages are preferable to one wall of text when the transport supports incremental messages. Keep related code or copyable text in its own fenced block.
- Use emoji sparingly and naturally. Do not add decorative emoji to every message.
- When the result is a document, Markdown file, table, diagram, image, or other artifact, create the real artifact in the exact per-turn directory supplied as $HUB_STAGING_DIR or in the ARTIFACT DELIVERY DIRECTORY section. Do not use a shared staging directory. The Hub validates and delivers eligible files as Telegram attachments.
- For a small closed choice, state concise option labels suitable for Telegram inline buttons.
- Never claim that a file, button, or reaction was sent unless the transport confirms it. The Hub, not you, owns Telegram UI delivery.
- Do not expose hidden reasoning, secrets, raw terminal screens, or unfiltered tool output. Visible progress should describe actions and outcomes, not private chain-of-thought.
- Treat Replies, forwarded quotes, and shared topic excerpts according to their explicit labels. Respond only to the current user turn.
"""

_REMINDER = """TELEGRAM TRANSPORT REMINDER v2
Reply for a Telegram conversation: concise, conversational, and outcome-first. Start simple work immediately; for complex work publish a brief understanding and approach when possible, then continue without an artificial delay. Ask only materially useful clarification questions. Put copyable text in a separate fenced block and stage deliverable files only in the exact per-turn directory supplied by the Hub. Never claim Telegram UI actions that the Hub has not confirmed. Do not expose hidden reasoning.
"""

_FULL_CONTRACT_V1 = (
    _FULL_CONTRACT.replace(
        "TELEGRAM INTERACTION CONTRACT v2", "TELEGRAM INTERACTION CONTRACT v1", 1
    )
    .replace(
        ", then proceed without an artificial delay",
        "",
        1,
    )
    .replace(
        "- Pause only for a material ambiguity, missing authority, destructive decision, explicit planning request, or a direct request to wait. Start, Clarify, and Cancel choices are for a real pending decision, not a mandatory pre-task ceremony.\n",
        "",
        1,
    )
)

_REMINDER_V1 = """TELEGRAM TRANSPORT REMINDER v1
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


def telegram_developer_instructions(*, runtime: str, new_session: bool) -> str:
    """Return stable Telegram instructions for a provider-native instruction channel."""

    if runtime == "codex":
        contract = _FULL_CONTRACT if new_session else _REMINDER
    else:
        contract = _FULL_CONTRACT_V1 if new_session else _REMINDER_V1
    runtime_note = _RUNTIME_NOTES.get(runtime)
    sections = [contract.strip()]
    if runtime_note is not None:
        sections.append(runtime_note)
    return "\n\n".join(sections)


def telegram_contract_version(runtime: str) -> int:
    """Return the accepted contract version for one runtime."""

    return CODEX_TELEGRAM_CONTRACT_VERSION if runtime == "codex" else TELEGRAM_CONTRACT_VERSION


def telegram_user_turn_prompt(user_turn: str, *, staging_dir: object | None = None) -> str:
    """Add only per-turn transport metadata to the user's productive text."""

    clean = user_turn.strip()
    if not clean:
        raise ValueError("Telegram user turn is empty")
    sections: list[str] = []
    if staging_dir is not None:
        sections.append(
            "ARTIFACT DELIVERY DIRECTORY FOR THIS TURN:\n"
            f"{staging_dir}\n"
            "Place only deliberate user-facing deliverables there. Files elsewhere are not "
            "attached, and files left by other turns are never reused."
        )
    sections.append(f"CURRENT USER TURN:\n{clean}")
    return "\n\n".join(sections)


def telegram_turn_prompt(
    user_turn: str,
    *,
    runtime: str,
    new_session: bool,
    staging_dir: object | None = None,
) -> str:
    """Prompt fallback for runtimes without a native instruction channel."""

    return "\n\n".join(
        (
            telegram_developer_instructions(runtime=runtime, new_session=new_session),
            telegram_user_turn_prompt(user_turn, staging_dir=staging_dir),
        )
    )
