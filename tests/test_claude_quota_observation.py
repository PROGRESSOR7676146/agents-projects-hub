from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from typing import Any, cast

from hermes_codex_router.claude_stream import (
    ClaudeStreamError,
    ClaudeStreamReader,
    parse_claude_stream,
)
from tests.test_claude_stream import SESSION, output, result


class ClaudeQuotaObservationTests(unittest.TestCase):
    def observe(
        self, *events: Mapping[str, object], file_policy: bool = False
    ) -> tuple[list[dict[str, object]], str]:
        observed: list[dict[str, object]] = []
        reader = ClaudeStreamReader(
            expected_session_id=SESSION,
            event_policy=(lambda _: None) if file_policy else None,
            on_quota_observation=observed.append,
        )
        reader.feed((output(*events) + "\n").encode())
        retained = reader.finish()
        # Persistence is outside the pipe drain and its productive deadline.
        self.assertEqual(observed, [])
        reader.publish_quota_observations()
        reader.publish_quota_observations()
        return observed, retained

    def test_allowlisted_nested_and_top_level_events_with_valid_resets(self) -> None:
        for event, expected, limit_type in (
            (
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {
                        "status": "rejected",
                        "rateLimitType": "five_hour",
                        "resetsAt": 1893456000,
                        "account": "private sentinel",
                    },
                    "message": "private sentinel",
                },
                "2030-01-01T00:00:00Z",
                "five_hour",
            ),
            (
                {
                    "type": "rate_limit_event",
                    "status": "rejected",
                    "rateLimitType": "seven_day",
                    "resetsAt": "2030-01-01T02:00:00+02:00",
                },
                "2030-01-01T00:00:00Z",
                "seven_day",
            ),
        ):
            with self.subTest(event=event):
                for file_policy in (False, True):
                    observed, retained = self.observe(event, result(), file_policy=file_policy)
                    self.assertEqual(
                        observed,
                        [
                            {
                                "event_type": "rate_limit_event",
                                "status": "rejected",
                                "rateLimitType": limit_type,
                                "reset_present": True,
                                "reset_at": expected,
                            }
                        ],
                    )
                    self.assertEqual(
                        parse_claude_stream(
                            retained, event_policy=(lambda _: None) if file_policy else None
                        ).text,
                        "Visible answer",
                    )

    def test_missing_reset_is_observed_without_a_timestamp(self) -> None:
        observed, _ = self.observe(
            {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}, result()
        )
        self.assertEqual(
            observed,
            [
                {
                    "event_type": "rate_limit_event",
                    "status": "rejected",
                    "rateLimitType": None,
                    "reset_present": False,
                }
            ],
        )

    def test_numeric_429_status_is_allowlisted(self) -> None:
        observed, _ = self.observe({"type": "rate_limit_event", "status": 429}, result())
        self.assertEqual(observed[0]["status"], 429)

    def test_explicit_quota_event_is_observed(self) -> None:
        observed, _ = self.observe(
            {"type": "quota_event", "status": "rejected", "message": "private sentinel"},
            result(),
        )
        self.assertEqual(
            observed,
            [
                {
                    "event_type": "quota_event",
                    "status": "rejected",
                    "rateLimitType": None,
                    "reset_present": False,
                }
            ],
        )

    def test_malformed_oversized_and_hostile_values_are_not_persisted(self) -> None:
        invalid = (
            None,
            True,
            False,
            -1,
            253402300800,
            10**100,
            float("inf"),
            float("nan"),
            {},
            [],
            "9" * 1000,
            "private sentinel",
            "2030-01-01T00:00:00Z private sentinel",
            "2030-01-01T00:00:00",
            "2030-01-01",
            "2030-99-99T00:00:00Z",
            "2030-01-01T00:00:00+02:99",
            "2030-01-01T00:00:00+99:00",
        )
        for value in invalid:
            with self.subTest(value=value):
                observed, _ = self.observe(
                    {
                        "type": "rate_limit_event",
                        "rate_limit_info": {
                            "resetsAt": value,
                            "status": "private sentinel",
                            "rateLimitType": "private sentinel",
                            "other": "private sentinel",
                        },
                        "session_id": SESSION,
                        "payload": "private sentinel",
                    },
                    result(),
                )
                self.assertEqual(
                    observed,
                    [
                        {
                            "event_type": "rate_limit_event",
                            "status": None,
                            "rateLimitType": None,
                            "reset_present": True,
                        }
                    ],
                )
                self.assertNotIn("private sentinel", json.dumps(observed))

    def test_quota_result_is_observed_but_unrelated_events_are_not(self) -> None:
        unrelated = {
            "type": "system",
            "status": "rejected",
            "resetsAt": 1893456000,
            "rateLimitType": "five_hour",
            "message": "private sentinel",
        }
        observed, retained = self.observe(unrelated, result())
        self.assertEqual(observed, [])
        self.assertEqual(parse_claude_stream(retained).text, "Visible answer")
        observed, _ = self.observe(
            unrelated,
            result(
                subtype="error_during_execution",
                is_error=True,
                api_error_status=429,
                resetsAt=1893456000,
                errors=["private sentinel"],
            ),
        )
        self.assertEqual(
            observed,
            [
                {
                    "event_type": "result",
                    "status": 429,
                    "rateLimitType": None,
                    "reset_present": True,
                    "reset_at": "2030-01-01T00:00:00Z",
                }
            ],
        )

    def test_explicit_429_is_observed_even_with_a_budget_terminal_subtype(self) -> None:
        observed, _ = self.observe(
            result(subtype="error_max_budget_usd", is_error=True, api_error_status=429)
        )
        self.assertEqual(len(observed), 1)
        self.assertEqual((observed[0]["event_type"], observed[0]["status"]), ("result", 429))

    def test_assistant_rate_limit_proves_quota_result_only_for_matching_latest_error(self) -> None:
        error = {"type": "assistant", "session_id": SESSION, "error": "rate_limit"}
        terminal = result(subtype="error_during_execution", is_error=True)
        observed, _ = self.observe(error, terminal)
        self.assertEqual([item["event_type"] for item in observed], ["assistant", "result"])
        observed, _ = self.observe(error, {"type": "assistant", "session_id": SESSION}, terminal)
        self.assertEqual([item["event_type"] for item in observed], ["assistant"])
        observed, _ = self.observe({"type": "assistant", "error": "rate_limit"}, terminal)
        self.assertEqual([item["event_type"] for item in observed], ["assistant"])

    def test_assistant_error_does_not_cross_correction_result_boundaries(self) -> None:
        class TwoResults:
            count = 0

            def observe(self, event: dict[str, object]) -> bool:
                if event.get("type") == "result":
                    self.count += 1
                    return self.count == 2
                return True

            def finish(self) -> None:
                pass

        observed: list[dict[str, object]] = []
        reader = ClaudeStreamReader(
            expected_session_id=SESSION,
            correction_input=cast(Any, TwoResults()),
            on_quota_observation=observed.append,
        )
        assistant = {"type": "assistant", "session_id": SESSION, "error": "rate_limit"}
        terminal = result(subtype="error_during_execution", is_error=True)
        reader.feed((output(assistant, terminal, terminal) + "\n").encode())
        reader.finish()
        reader.publish_quota_observations()
        self.assertEqual([item["event_type"] for item in observed], ["assistant", "result"])

    def test_observation_is_bounded_and_preserves_terminal_evidence(self) -> None:
        rates = [
            {"type": "rate_limit_event", "status": "rejected", "resetsAt": 1893456000 + index}
            for index in range(20)
        ]
        terminal = result(subtype="error_during_execution", is_error=True, api_error_status=429)
        observed, _ = self.observe(*rates, terminal)
        self.assertEqual(len(observed), 8)
        self.assertEqual(observed[0]["reset_at"], "2030-01-01T00:00:13Z")
        self.assertEqual(observed[-2]["reset_at"], "2030-01-01T00:00:19Z")
        self.assertEqual(observed[-1]["event_type"], "result")

    def test_event_after_terminal_does_not_create_a_quota_observation(self) -> None:
        observed: list[dict[str, object]] = []
        reader = ClaudeStreamReader(
            expected_session_id=SESSION, on_quota_observation=observed.append
        )
        with self.assertRaisesRegex(ClaudeStreamError, "after its terminal"):
            reader.feed((output(result(), {"type": "rate_limit_event"}) + "\n").encode())
        reader.publish_quota_observations()
        self.assertEqual(observed, [])

    def test_observation_sink_failure_does_not_change_result(self) -> None:
        def fail(_: object) -> None:
            raise RuntimeError("private sink failure")

        reader = ClaudeStreamReader(expected_session_id=SESSION, on_quota_observation=fail)
        reader.feed((output({"type": "rate_limit_event"}, result()) + "\n").encode())
        retained = reader.finish()
        reader.publish_quota_observations()
        self.assertEqual(parse_claude_stream(retained).text, "Visible answer")

    def test_quota_result_is_observed_even_if_an_input_receipt_rejects_it(self) -> None:
        class RejectingReceipt:
            def observe(self, _: object) -> bool:
                raise ClaudeStreamError("fictional receipt is unverified")

        observed: list[dict[str, object]] = []
        reader = ClaudeStreamReader(
            expected_session_id=SESSION,
            correction_input=cast(Any, RejectingReceipt()),
            on_quota_observation=observed.append,
        )
        with self.assertRaises(ClaudeStreamError):
            reader.feed(
                (
                    output(
                        result(
                            subtype="error_during_execution", is_error=True, api_error_status=429
                        )
                    )
                    + "\n"
                ).encode()
            )
        reader.publish_quota_observations()
        self.assertEqual([item["event_type"] for item in observed], ["result"])


if __name__ == "__main__":
    unittest.main()
