"""Substitutions must refuse before the finite selection endpoint serves output."""

from __future__ import annotations

import copy
import json
import unittest
from dataclasses import replace
from typing import cast

from tests.claude_native_request_contract import (
    NativeRequestContractError,
    environment_text,
)
from tests.claude_native_transport_actor import FixtureServer
from tests.claude_selection_actor import endpoint
from tests.claude_selection_contract import (
    CASES,
    MARKERS,
    PROMPTS,
    SONNET,
    SONNET_BETA,
    SelectionRequest,
    selection,
    validate_selection_body,
    validate_selection_headers,
)
from tests.test_claude_native_request_contract import base_headers, request, valid_body

OS = "Linux 0.0.0-example"
DATE = "2000-01-01"


def fixture_body(case: str, phase: int) -> dict:
    """Hand-built synthetic requests: never call the oracle's message builder."""
    body = valid_body()
    model, effort = selection(case, phase)
    opus = environment_text(OS, DATE)
    sonnet = opus.replace(
        "Opus 5.5. The exact model ID is claude-opus-5-5. Assistant knowledge cutoff is June 2026.",
        "Sonnet 4.6. The exact model ID is claude-sonnet-4-6. Assistant knowledge cutoff is August 2025.",
    )
    cached = {"type": "ephemeral"}

    def text(value):
        return {"type": "text", "text": value}

    assistant = {"role": "assistant", "content": [text(MARKERS[0])]}
    if model == SONNET:
        prior = (opus if case == "opus-sonnet" else sonnet).split("\n\n")
        reminders = [
            text("<system-reminder>\n" + value + "\n</system-reminder>") for value in prior
        ]
        reminders[-1]["text"] += "\n"
        first = {"role": "user", "content": reminders + [text(PROMPTS[0])]}
        if phase == 0:
            first["content"][-1]["cache_control"] = cached
            messages = [first]
        else:
            current = [
                text("<system-reminder>\n" + sonnet.split("\n\n")[1] + "\n</system-reminder>"),
                text(
                    "<system-reminder>\n<total_tokens>15000000 tokens left</total_tokens>\n</system-reminder>\n"
                ),
                {**text(PROMPTS[1]), "cache_control": cached},
            ]
            messages = [first, assistant, {"role": "user", "content": current}]
    elif phase == 0:
        messages = [
            {"role": "user", "content": PROMPTS[0]},
            {
                "role": "system",
                "content": [{**text(opus), "cache_control": cached}],
                "output_config": {"effort": "high"},
            },
        ]
    else:
        current = "<total_tokens>15000000 tokens left</total_tokens>"
        if case == "sonnet-opus":
            current = opus.split("\n\n")[1] + "\n\n" + current
        messages = [
            {"role": "user", "content": PROMPTS[0]},
            {
                "role": "system",
                "content": sonnet if case == "sonnet-opus" else opus,
                "output_config": {"effort": "high"},
            },
            assistant,
            {"role": "user", "content": PROMPTS[1]},
            {
                "role": "system",
                "content": [{**text(current), "cache_control": cached}],
                **({"output_config": {"effort": "medium"}} if case == "opus-effort" else {}),
            },
        ]
    body.update(model=model, messages=messages, output_config={"effort": effort})
    return body


class SelectionContractTests(unittest.TestCase):
    def assert_refused(self, body: dict, expected: SelectionRequest) -> None:
        raw = json.dumps(body).encode()
        with self.assertRaises(NativeRequestContractError):
            validate_selection_body(raw, expected)
        with endpoint(0, expected) as server:
            headers = base_headers(server, raw)
            if selection(expected.case, expected.phase)[0] == SONNET:
                headers = [
                    (key, SONNET_BETA if key.lower() == "anthropic-beta" else value)
                    for key, value in headers
                ]
            self.assertEqual(request(server, raw, headers), (400, b""))
        self.assertEqual(server.posts, 1)
        self.assertEqual(server.validated_requests, 0)
        self.assertEqual(server.messages_served, 0)
        self.assertEqual(server.violations, 1)

    def test_all_six_independent_phase_shapes_pass(self):
        for case in CASES:
            for phase in (0, 1):
                validate_selection_body(
                    json.dumps(fixture_body(case, phase)).encode(),
                    SelectionRequest(case, phase, OS, DATE),
                )

    def test_each_positive_shape_serves_only_its_prepared_marker(self):
        for case in CASES:
            for phase in (0, 1):
                raw = json.dumps(fixture_body(case, phase)).encode()
                with endpoint(0, SelectionRequest(case, phase, OS, DATE)) as server:
                    headers = base_headers(server, raw)
                    if selection(case, phase)[0] == SONNET:
                        headers = [
                            (key, SONNET_BETA if key.lower() == "anthropic-beta" else value)
                            for key, value in headers
                        ]
                    code, reply = request(server, raw, headers)
                    self.assertEqual(code, 200)
                    self.assertIn(MARKERS[phase].encode(), reply)
                self.assertEqual(server.validated_requests, 1)

    def test_invalid_first_post_cannot_be_replaced_by_a_valid_request(self):
        expected = SelectionRequest("opus-effort", 1, OS, DATE)
        body = fixture_body(expected.case, expected.phase)
        with endpoint(0, expected) as server:
            body["output_config"] = {"effort": "high"}
            self.assertEqual(request(server, json.dumps(body).encode()), (400, b""))
            self.assertEqual(
                request(server, json.dumps(fixture_body(expected.case, expected.phase)).encode()),
                (400, b""),
            )
        self.assertEqual(server.validated_requests, 0)
        self.assertEqual(server.messages_served, 0)

    def test_model_effort_tools_metadata_and_extras_refuse(self):
        for case in CASES:
            for phase in (0, 1):
                expected = SelectionRequest(case, phase, OS, DATE)
                for key, value in (
                    ("model", "example-wrong"),
                    ("output_config", {"effort": "low"}),
                    ("tools", ["Read"]),
                    ("extra", True),
                    ("thinking", {"type": "enabled"}),
                ):
                    body = fixture_body(case, phase)
                    body[key] = value
                    self.assert_refused(body, expected)
                body = fixture_body(case, phase)
                metadata = json.loads(body["metadata"]["user_id"])
                metadata["session_id"] = "019abcde-1234-7fff-8fff-0123456789ab"
                body["metadata"]["user_id"] = json.dumps(metadata)
                self.assert_refused(body, expected)

    def test_history_deletion_reordering_and_duplication_refuse(self):
        for case in CASES:
            expected = SelectionRequest(case, 1, OS, DATE)
            original = fixture_body(case, 1)
            for index in range(len(original["messages"])):
                body = copy.deepcopy(original)
                del body["messages"][index]
                self.assert_refused(body, expected)
            for messages in (
                original["messages"][::-1],
                original["messages"] + [original["messages"][0]],
            ):
                body = copy.deepcopy(original)
                body["messages"] = messages
                self.assert_refused(body, expected)

    def test_every_history_string_and_historical_effort_are_exact(self):
        def leaves(value, path=()):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield from leaves(item, path + (key,))
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    yield from leaves(item, path + (index,))
            elif isinstance(value, str):
                yield path

        for case in CASES:
            expected = SelectionRequest(case, 1, OS, DATE)
            original = fixture_body(case, 1)
            for path in leaves(original["messages"]):
                body = copy.deepcopy(original)
                target = body["messages"]
                for part in path[:-1]:
                    target = target[part]
                target[path[-1]] += "example-changed"
                self.assert_refused(body, expected)

    def test_malformed_expectations_and_phase_order_refuse(self):
        good = SelectionRequest(CASES[0], 1, OS, DATE)
        for bad in (
            replace(good, case="example-unknown"),
            replace(good, phase=True),
            replace(good, phase=2),
            replace(good, os_version=""),
            replace(good, date="not-a-date"),
            {**good.__dict__, "extra": True},
        ):
            with self.assertRaises(NativeRequestContractError):
                validate_selection_body(
                    json.dumps(fixture_body(CASES[0], 1)).encode(), cast(SelectionRequest, bad)
                )
        self.assert_refused(fixture_body(CASES[0], 0), good)

    def test_headers_refuse_foreign_uuid_beta_and_duplicates(self):
        # The header fixture is independent of the selected native oracle.
        class Server:
            server_port = 12345
            case = "api-key-success"

        expected = SelectionRequest("opus-effort", 1, OS, DATE)
        raw = json.dumps(fixture_body(expected.case, expected.phase)).encode()
        good = base_headers(cast(FixtureServer, Server()), raw)
        validate_selection_headers(good, port=12345, expected=expected)
        for headers in (
            good + [good[0]],
            [
                (
                    k,
                    "example-wrong"
                    if k.lower() in {"anthropic-beta", "x-claude-code-session-id"}
                    else v,
                )
                for k, v in good
            ],
        ):
            with self.assertRaises(NativeRequestContractError):
                validate_selection_headers(headers, port=12345, expected=expected)

    def test_duplicate_json_valid_last_value_cannot_hide_substitution(self):
        expected = SelectionRequest("opus-effort", 1, OS, DATE)
        raw = json.dumps(fixture_body(expected.case, expected.phase)).encode()
        raw = raw.replace(b'"model":', b'"model":"example-other","model":', 1)
        with self.assertRaises(NativeRequestContractError):
            validate_selection_body(raw, expected)
