#!/usr/bin/env python3
"""Export only visible user/assistant messages from a Codex rollout JSONL."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

SENSITIVE_PATTERNS = (
    (re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"), "[REDACTED TELEGRAM TOKEN]"),
    (re.compile(r"https://t\.me/\+[A-Za-z0-9_-]+"), "[REDACTED PRIVATE INVITE]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]{20,}"), "Bearer [REDACTED]"),
)


def sanitize(text: str) -> str:
    for pattern, replacement in SENSITIVE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def visible_text(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in payload.get("content", []):
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {"input_text", "output_text"}:
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(sanitize(text.strip()))
    return "\n\n".join(parts)


def export(source: Path, destination: Path) -> int:
    messages: list[tuple[str, str, str]] = []
    with source.open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            payload = record.get("payload", {})
            if record.get("type") != "response_item" or payload.get("type") != "message":
                continue
            role = payload.get("role")
            if role not in {"user", "assistant"}:
                continue
            text = visible_text(payload)
            if text:
                messages.append((str(record.get("timestamp", "")), str(role), text))

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        handle.write("# Visible session history\n\n")
        handle.write(
            "Sanitized export of visible user/assistant messages. System and developer "
            "instructions, hidden reasoning, tool calls, tool output, environment dumps, "
            "credentials, and raw approval payloads are intentionally excluded.\n\n"
        )
        handle.write(f"Source rollout: `{source.name}`\n\n")
        for index, (timestamp, role, text) in enumerate(messages, start=1):
            handle.write(f"## {index}. {role.title()} · {timestamp}\n\n{text}\n\n")
    return len(messages)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    count = export(args.source.expanduser().resolve(strict=True), args.destination.resolve())
    print(json.dumps({"ok": True, "messages": count, "destination": str(args.destination)}))


if __name__ == "__main__":
    main()
