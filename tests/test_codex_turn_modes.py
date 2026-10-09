from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.codex_appserver import CodexAppServerClient, RateLimits, TurnResult
from hermes_codex_router.codex_turn_modes import MAX_EARLY_GOAL_TURNS, CodexTurnModes
from hermes_codex_router.incoming_materials import PreparedIncomingMaterials
from hermes_codex_router.metadata import format_telegram_response
from hermes_codex_router.worker_execution import prepare_codex_worker_result
from tests.test_codex_appserver import FakeTransport


def goal_event(status: object = "active", *, turn: object = "example-turn") -> dict:
    return {
        "method": "thread/goal/updated",
        "params": {
            "threadId": "example-thread",
            "turnId": turn,
            "goal": {"threadId": "example-thread", "status": status, "objective": "PRIVATE"},
        },
    }


def completed() -> dict:
    return {
        "method": "turn/completed",
        "params": {
            "threadId": "example-thread",
            "turn": {"id": "example-turn", "status": "completed"},
        },
    }


class CodexTurnModeTests(unittest.TestCase):
    def tracker(self) -> CodexTurnModes:
        tracker = CodexTurnModes()
        tracker.begin("example-thread")
        tracker.accept("example-turn")
        return tracker

    def test_exact_goal_statuses_remain_distinct_without_retaining_objective(self) -> None:
        for status in ("active", "paused", "blocked", "usageLimited", "budgetLimited", "complete"):
            with self.subTest(status=status):
                tracker = self.tracker()
                tracker.observe(goal_event(status))
                self.assertEqual(tracker.snapshot().goal_status, status)
                self.assertNotIn("PRIVATE", repr(tracker.snapshot()))

    def test_foreign_thread_turn_and_server_request_do_not_replace_exact_status(self) -> None:
        tracker = self.tracker()
        tracker.observe(goal_event())
        wrong_thread = goal_event("paused")
        wrong_thread["params"]["threadId"] = "example-other-thread"
        server_request = goal_event("paused")
        server_request["id"] = 42
        for event in (
            wrong_thread,
            goal_event("paused", turn="example-other-turn"),
            server_request,
        ):
            tracker.observe(event)
            self.assertEqual(tracker.snapshot().goal_status, "active")

    def test_early_events_are_selected_only_after_exact_acceptance(self) -> None:
        tracker = CodexTurnModes()
        tracker.begin("example-thread")
        tracker.observe(goal_event("complete", turn="example-old-turn"))
        tracker.observe(goal_event("active"))
        self.assertIsNone(tracker.snapshot().goal_status)
        tracker.accept("example-turn")
        self.assertEqual(tracker.snapshot().goal_status, "active")
        tracker.observe(goal_event("paused"))
        self.assertEqual(tracker.snapshot().goal_status, "paused")

    def test_cleared_and_unbound_updates_invalidate_without_claiming_disabled(self) -> None:
        for event in (
            {"method": "thread/goal/cleared", "params": {"threadId": "example-thread"}},
            goal_event("paused", turn=None),
            goal_event("paused", turn=""),
            goal_event("paused", turn=[]),
        ):
            with self.subTest(event=event):
                tracker = self.tracker()
                tracker.observe(goal_event())
                tracker.observe(event)
                self.assertIsNone(tracker.snapshot().goal_status)

    def test_malformed_exact_update_clears_known_status_without_throwing(self) -> None:
        for goal in (None, [], {}, {"threadId": "example-other-thread", "status": "active"}):
            tracker = self.tracker()
            tracker.observe(goal_event())
            event = goal_event()
            event["params"]["goal"] = goal
            tracker.observe(event)
            self.assertIsNone(tracker.snapshot().goal_status)
        for status in (None, [], {}, True, "invented", "<b>active</b>"):
            tracker = self.tracker()
            tracker.observe(goal_event())
            tracker.observe(goal_event(status))
            self.assertIsNone(tracker.snapshot().goal_status)

    def test_clear_during_submission_cannot_restore_an_earlier_candidate(self) -> None:
        tracker = CodexTurnModes()
        tracker.begin("example-thread")
        tracker.observe(goal_event())
        tracker.observe({"method": "thread/goal/cleared", "params": {"threadId": "example-thread"}})
        tracker.accept("example-turn")
        self.assertIsNone(tracker.snapshot().goal_status)

    def test_next_turn_and_failed_submission_reset_observation(self) -> None:
        tracker = self.tracker()
        tracker.observe(goal_event())
        previous = tracker.snapshot()
        tracker.begin("example-thread")
        tracker.accept("example-next-turn")
        self.assertIsNone(tracker.snapshot().goal_status)
        tracker.reset()
        tracker.observe(goal_event())
        self.assertIsNone(tracker.snapshot().goal_status)
        self.assertEqual(previous.goal_status, "active")

    def test_early_bound_retires_only_optional_metadata_and_cannot_reopen(self) -> None:
        tracker = CodexTurnModes()
        tracker.begin("example-thread")
        for index in range(MAX_EARLY_GOAL_TURNS + 1):
            tracker.observe(goal_event(turn=f"example-candidate-{index}"))
        tracker.observe({"method": "thread/goal/cleared", "params": {"threadId": "example-thread"}})
        tracker.accept("example-turn")
        tracker.observe(goal_event())
        self.assertIsNone(tracker.snapshot().goal_status)
        tracker.begin("example-thread")
        tracker.accept("example-turn")
        tracker.observe(goal_event())
        self.assertEqual(tracker.snapshot().goal_status, "active")

    def test_missing_thread_and_oversized_ids_never_create_a_mode_claim(self) -> None:
        for params in (None, [], {}, {"turnId": "example-turn"}, {"threadId": []}):
            tracker = self.tracker()
            tracker.observe({"method": "thread/goal/updated", "params": params})
            self.assertIsNone(tracker.snapshot().goal_status)
        tracker = CodexTurnModes()
        tracker.begin("example-thread")
        tracker.observe(goal_event(turn="x" * 257))
        tracker.accept("x" * 257)
        self.assertIsNone(tracker.snapshot().goal_status)


class CodexTurnModePipelineTests(unittest.TestCase):
    def run_turn(self, events: list[dict]) -> tuple[TurnResult, FakeTransport]:
        transport = FakeTransport(events)
        client = CodexAppServerClient(transport, initialized=True)
        with tempfile.TemporaryDirectory() as directory:
            turn_id = client.start_turn(
                thread_id="example-thread",
                cwd=Path(directory),
                text="/goal and /fast",
                model="gpt-example",
                effort="high",
            )
            return client.wait_for_turn(turn_id), transport

    def render(self, result: TurnResult) -> str:
        return format_telegram_response(
            result=result,
            agent="Codex",
            model="gpt-example",
            effort="high",
            session_label="Example Project · General",
            limits=RateLimits(None, None),
            timezone_name="UTC",
        )

    def test_early_flood_keeps_exact_final_context_and_goal_without_buffer_growth(self) -> None:
        final = {
            "method": "item/completed",
            "params": {
                "threadId": "example-thread",
                "turnId": "example-turn",
                "item": {
                    "id": "example-final",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "Saved final",
                },
            },
        }
        result, transport = self.run_turn(
            [
                *[goal_event("active") for _ in range(1100)],
                {
                    "method": "thread/tokenUsage/updated",
                    "params": {
                        "threadId": "example-thread",
                        "turnId": "example-turn",
                        "tokenUsage": {"modelContextWindow": 1000, "last": {"totalTokens": 200}},
                    },
                },
                final,
                completed(),
                {"id": 1, "result": {"turn": {"id": "example-turn"}}},
            ]
        )
        self.assertEqual(result.text, "Saved final")
        self.assertEqual(result.context_tokens_used, 200)
        self.assertIn("Modes: /goal (active, observed)", self.render(result))
        self.assertIn("Context remaining: 80.0%", self.render(result))
        self.assertEqual([message["method"] for message in transport.sent], ["turn/start"])

    def test_worker_notices_preserve_snapshot_and_approval_authority_is_unchanged(self) -> None:
        transport = FakeTransport(
            [
                goal_event(),
                {
                    "method": "item/commandExecution/requestApproval",
                    "id": 44,
                    "params": {"threadId": "example-thread", "turnId": "example-turn"},
                },
                {"id": 1, "result": {"turn": {"id": "example-turn"}}},
                completed(),
            ]
        )
        client = CodexAppServerClient(transport, initialized=True, approval_policy="never")
        with tempfile.TemporaryDirectory() as directory:
            turn = client.start_turn(
                thread_id="example-thread",
                cwd=Path(directory),
                text="Task",
                model="gpt-example",
                effort="high",
            )
            result = client.wait_for_turn(turn)
        prepared = prepare_codex_worker_result(
            result,
            PreparedIncomingMaterials("", (), ("Example unavailable material",), None, ()),
            agent_name="Codex",
            model="gpt-example",
            effort="high",
            session_label="Example Project · General",
            limits=RateLimits(None, None),
            artifact_notice="Artifact notice",
        )
        self.assertIn("/goal (active, observed)", prepared.telegram_html)
        self.assertIn("Example unavailable material", prepared.telegram_html)
        self.assertEqual(transport.sent[1], {"id": 44, "result": {"decision": "decline"}})
        self.assertEqual(len(transport.sent), 2)

    def test_goal_bound_exhaustion_still_delivers_completion(self) -> None:
        result, _ = self.run_turn(
            [
                *[
                    goal_event(turn=f"example-candidate-{index}")
                    for index in range(MAX_EARLY_GOAL_TURNS + 1)
                ],
                {"id": 1, "result": {"turn": {"id": "example-turn"}}},
                goal_event(),
                completed(),
            ]
        )
        self.assertNotIn("Modes:", self.render(result))

    def test_prompt_and_unbound_settings_do_not_invent_goal_or_fast(self) -> None:
        result, _ = self.run_turn(
            [
                {"id": 1, "result": {"turn": {"id": "example-turn"}}},
                goal_event(turn=None),
                {
                    "method": "thread/settings/updated",
                    "params": {
                        "threadId": "example-thread",
                        "threadSettings": {"serviceTier": "fast"},
                    },
                },
                completed(),
            ]
        )
        self.assertNotIn("Modes:", self.render(result))

    def test_completion_only_recovery_has_no_current_mode_claim(self) -> None:
        transport = FakeTransport(
            [
                {
                    "id": 1,
                    "result": {
                        "thread": {
                            "id": "example-thread",
                            "cwd": "PLACEHOLDER",
                            "turns": [
                                {
                                    "id": "example-turn",
                                    "status": "completed",
                                    "items": [
                                        {
                                            "id": "example-final",
                                            "type": "agentMessage",
                                            "text": "Saved final",
                                        }
                                    ],
                                }
                            ],
                            "goal": {"status": "active"},
                            "serviceTier": "fast",
                        }
                    },
                }
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            transport.incoming[0]["result"]["thread"]["cwd"] = directory
            transport.incoming.append({"id": 2, "result": transport.incoming[0]["result"]})
            client = CodexAppServerClient(transport, initialized=True)
            outcome = client.read_turn_outcome(
                thread_id="example-thread", turn_id="example-turn", cwd=Path(directory)
            )
        self.assertIsNotNone(outcome.result)
        assert outcome.result is not None
        self.assertNotIn("Modes:", self.render(outcome.result))

    def test_latest_observation_may_follow_buffered_completion_before_acceptance(self) -> None:
        result, _ = self.run_turn(
            [
                goal_event(),
                completed(),
                goal_event("paused"),
                {"id": 1, "result": {"turn": {"id": "example-turn"}}},
            ]
        )
        self.assertIn("/goal (paused, observed)", self.render(result))

    def test_reused_client_does_not_carry_goal_to_a_later_turn(self) -> None:
        transport = FakeTransport(
            [
                {"id": 1, "result": {"turn": {"id": "example-turn"}}},
                goal_event(),
                completed(),
                {"id": 2, "result": {"turn": {"id": "example-turn"}}},
                completed(),
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)
        with tempfile.TemporaryDirectory() as directory:
            first = client.start_turn(
                thread_id="example-thread",
                cwd=Path(directory),
                text="Task",
                model="gpt-example",
                effort="high",
            )
            first_result = client.wait_for_turn(first)
            second = client.start_turn(
                thread_id="example-thread",
                cwd=Path(directory),
                text="Next",
                model="gpt-example",
                effort="high",
            )
            second_result = client.wait_for_turn(second)
        self.assertIn("/goal (active, observed)", self.render(first_result))
        self.assertNotIn("Modes:", self.render(second_result))

    def test_rejected_submission_and_thread_switch_do_not_reuse_early_status(self) -> None:
        transport = FakeTransport(
            [
                goal_event(),
                {"id": 1, "error": {"message": "Example rejection"}},
                {"id": 2, "result": {"turn": {"id": "example-turn"}}},
                goal_event(),
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "example-next-thread",
                        "turn": {"id": "example-turn", "status": "completed"},
                    },
                },
            ]
        )
        client = CodexAppServerClient(transport, initialized=True)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(Exception, "Example rejection"):
                client.start_turn(
                    thread_id="example-thread",
                    cwd=Path(directory),
                    text="Task",
                    model="gpt-example",
                    effort="high",
                )
            turn = client.start_turn(
                thread_id="example-next-thread",
                cwd=Path(directory),
                text="Next",
                model="gpt-example",
                effort="high",
            )
            result = client.wait_for_turn(turn)
        self.assertNotIn("Modes:", self.render(result))

    def test_bare_wait_and_unsafe_snapshot_omit_unknown_modes(self) -> None:
        from hermes_codex_router.codex_turn_modes import CodexModeSnapshot

        transport = FakeTransport([goal_event(), completed()])
        client = CodexAppServerClient(transport, initialized=True)
        result = client.wait_for_turn("example-turn")
        self.assertNotIn("Modes:", self.render(result))
        rendered = self.render(TurnResult("Done", None, None, CodexModeSnapshot("<b>invented</b>")))
        self.assertNotIn("invented", rendered)

    def test_mismatched_wait_does_not_attach_the_accepted_turn_goal(self) -> None:
        for early in (True, False):
            with self.subTest(early=early):
                acceptance = {"id": 1, "result": {"turn": {"id": "example-turn"}}}
                other_completion = completed()
                other_completion["params"]["turn"]["id"] = "example-other-turn"
                goal = goal_event()
                events = [goal, acceptance] if early else [acceptance, goal]
                transport = FakeTransport(
                    [
                        *events,
                        {
                            "method": "item/completed",
                            "params": {
                                "threadId": "example-thread",
                                "turnId": "example-other-turn",
                                "item": {
                                    "id": "example-other-final",
                                    "type": "agentMessage",
                                    "phase": "final_answer",
                                    "text": "Other final",
                                },
                            },
                        },
                        other_completion,
                    ]
                )
                client = CodexAppServerClient(transport, initialized=True)
                with tempfile.TemporaryDirectory() as directory:
                    accepted = client.start_turn(
                        thread_id="example-thread",
                        cwd=Path(directory),
                        text="Task",
                        model="gpt-example",
                        effort="high",
                    )
                    self.assertEqual(accepted, "example-turn")
                    result = client.wait_for_turn("example-other-turn")
                self.assertEqual(result.text, "Other final")
                self.assertIsNone(result.modes)
                self.assertNotIn("Modes:", self.render(result))
                self.assertEqual([message["method"] for message in transport.sent], ["turn/start"])


if __name__ == "__main__":
    unittest.main()
