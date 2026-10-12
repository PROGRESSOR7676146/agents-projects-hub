"""Finite pinned test expectations, independent of argv and received native data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any, Sequence

from tests.claude_native_request_contract import (
    MODEL,
    POST_HEADERS,
    PROMPT,
    SUPPORTED_VERSION,
    ExpectedNativeRequest,
    NativeRequestContractError,
    _strict_json,
    environment_text,
    validate_headers,
    validate_request_body,
)

SONNET = "claude-sonnet-4-6"
CASES = ("opus-sonnet", "sonnet-opus", "opus-effort")
PROMPTS = ("Example selected text phase 0.", "Example selected text phase 1.")
MARKERS = ("example-selection-fresh", "example-selection-resume")
SONNET_BETA = (
    "claude-code-20250219,interleaved-thinking-2025-05-14,thinking-token-count-2026-05-13,"
    "context-management-2025-06-27,prompt-caching-scope-2026-01-05,effort-2025-11-24"
)
MODEL_TEXT = {
    MODEL: "You are powered by the model named Opus 5.5. The exact model ID is claude-opus-5-5. Assistant knowledge cutoff is June 2026.",
    SONNET: "You are powered by the model named Sonnet 4.6. The exact model ID is claude-sonnet-4-6. Assistant knowledge cutoff is August 2025.",
}
TOTAL = "<total_tokens>15000000 tokens left</total_tokens>"


@dataclass(frozen=True)
class SelectionRequest:
    case: str
    phase: int
    os_version: str
    date: str


def selection(case: str, phase: int) -> tuple[str, str]:
    if (
        not isinstance(case, str)
        or case not in CASES
        or type(phase) is not int
        or phase not in (0, 1)
    ):
        raise NativeRequestContractError("native_selection_invalid")
    return {
        "opus-sonnet": ((MODEL, "high"), (SONNET, "medium")),
        "sonnet-opus": ((SONNET, "medium"), (MODEL, "high")),
        "opus-effort": ((MODEL, "high"), (MODEL, "medium")),
    }[case][phase]


def validate_expectation(expected: SelectionRequest) -> None:
    if (
        type(expected) is not SelectionRequest
        or set(vars(expected)) != {"case", "phase", "os_version", "date"}
        or not isinstance(expected.os_version, str)
        or not 1 <= len(expected.os_version) <= 128
        or not expected.os_version.isascii()
        or any(ord(char) < 32 or ord(char) == 127 for char in expected.os_version)
        or not isinstance(expected.date, str)
        or len(expected.date) != 10
    ):
        raise NativeRequestContractError("native_selection_invalid")
    selection(expected.case, expected.phase)
    try:
        parsed_date = date.fromisoformat(expected.date)
    except ValueError:
        parsed_date = None
    if parsed_date is None or parsed_date.isoformat() != expected.date:
        raise NativeRequestContractError("native_selection_invalid")


def expected_selection_messages(expected: SelectionRequest) -> list[dict[str, Any]]:
    validate_expectation(expected)
    model, effort = selection(expected.case, expected.phase)
    prior_model, _ = selection(expected.case, 0)
    cached = {"type": "ephemeral"}

    def text(value: str) -> dict[str, Any]:
        return {"type": "text", "text": value}

    prefix = environment_text(expected.os_version, expected.date).split("\n\n")[0]

    def scaffold(selected_model: str) -> str:
        return "\n\n".join(
            (prefix, MODEL_TEXT[selected_model], TOTAL, "Today's date is " + expected.date + ".")
        )

    if model == SONNET:
        blocks = [
            text("<system-reminder>\n" + part + "\n</system-reminder>")
            for part in scaffold(prior_model).split("\n\n")
        ]
        blocks[-1]["text"] += "\n"
        blocks.append(text(PROMPTS[0]))
        first = {"role": "user", "content": blocks}
        if expected.phase == 0:
            blocks[-1]["cache_control"] = cached
            return [first]
        return [
            first,
            {"role": "assistant", "content": [text(MARKERS[0])]},
            {
                "role": "user",
                "content": [
                    text("<system-reminder>\n" + MODEL_TEXT[SONNET] + "\n</system-reminder>"),
                    text("<system-reminder>\n" + TOTAL + "\n</system-reminder>\n"),
                    {**text(PROMPTS[1]), "cache_control": cached},
                ],
            },
        ]
    if expected.phase == 0:
        return [
            {"role": "user", "content": PROMPTS[0]},
            {
                "role": "system",
                "content": [{**text(scaffold(model)), "cache_control": cached}],
                "output_config": {"effort": effort},
            },
        ]
    current = TOTAL if expected.case == "opus-effort" else MODEL_TEXT[MODEL] + "\n\n" + TOTAL
    return [
        {"role": "user", "content": PROMPTS[0]},
        # Sonnet history is reconstructed with current high by this pinned CLI;
        # this is compatibility, never proof that old effective effort changed.
        {"role": "system", "content": scaffold(prior_model), "output_config": {"effort": "high"}},
        {"role": "assistant", "content": [text(MARKERS[0])]},
        {"role": "user", "content": PROMPTS[1]},
        {
            "role": "system",
            "content": [{**text(current), "cache_control": cached}],
            **({"output_config": {"effort": effort}} if expected.case == "opus-effort" else {}),
        },
    ]


def validate_selection_body(raw: bytes, expected: SelectionRequest) -> None:
    messages = expected_selection_messages(expected)
    model, effort = selection(expected.case, expected.phase)
    body = _strict_json(raw)
    if (
        not isinstance(body, dict)
        or body.get("model") != model
        or body.get("output_config") != {"effort": effort}
        or body.get("messages") != messages
    ):
        raise NativeRequestContractError("native_selection_invalid")
    # Project only fields fully checked against independent finite expectations.
    # Every other field passes unchanged through the original full-body oracle.
    environment = environment_text(expected.os_version, expected.date)
    body.update(
        model=MODEL,
        output_config={"effort": "high"},
        messages=[
            {"role": "user", "content": PROMPT},
            {
                "role": "system",
                "content": [
                    {"type": "text", "text": environment, "cache_control": {"type": "ephemeral"}}
                ],
                "output_config": {"effort": "high"},
            },
        ],
    )
    validate_request_body(
        json.dumps(body).encode(), ExpectedNativeRequest(SUPPORTED_VERSION, environment)
    )


def validate_selection_headers(
    pairs: Sequence[tuple[str, str]], *, port: int, expected: SelectionRequest
) -> int:
    validate_expectation(expected)
    model, _ = selection(expected.case, expected.phase)
    if (
        not isinstance(pairs, (list, tuple))
        or len(pairs) > 24
        or any(
            not isinstance(pair, (tuple, list))
            or len(pair) != 2
            or any(not isinstance(item, str) for item in pair)
            for pair in pairs
        )
    ):
        raise NativeRequestContractError("native_selection_invalid")
    beta = [value for key, value in pairs if key.lower() == "anthropic-beta"]
    if beta != [SONNET_BETA if model == SONNET else POST_HEADERS["anthropic-beta"]]:
        raise NativeRequestContractError("native_selection_invalid")
    projected = [
        (key, POST_HEADERS["anthropic-beta"] if key.lower() == "anthropic-beta" else value)
        for key, value in pairs
    ]
    return validate_headers(projected, port=port, case="api-key-success", method="POST")
