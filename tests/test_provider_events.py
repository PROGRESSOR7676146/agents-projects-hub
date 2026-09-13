from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.codex_accounts import CodexAccountStatus, CodexPoolStatus
from hermes_codex_router.provider_events import (
    CodexRotationObservation,
    CodexRuntimeSnapshot,
    codex_rotation_targets,
    detect_codex_rotation,
    format_codex_rotation_event,
    read_codex_runtime_snapshot,
)
from hermes_codex_router.state import HubState


def account(index: int, *, active: bool, remaining: int, hint: str):
    return CodexAccountStatus(
        index, active, "available", "low", remaining, 50, None, None, None, False, hint
    )


class ProviderEventTests(unittest.TestCase):
    def test_reads_provider_429_counter_and_zero_based_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime-observability.json").write_text(
                json.dumps(
                    {
                        "lastAccountIndex": 1,
                        "runtimeMetrics": {
                            "rateLimitedResponses": 3,
                            "accountRotations": 4,
                        },
                    }
                ),
                encoding="utf-8",
            )
            value = read_codex_runtime_snapshot(root)
        assert value is not None
        self.assertEqual(value.rate_limited_responses, 3)
        self.assertEqual(value.active_account_index, 2)

    def test_rotation_message_contains_masked_source_and_target(self) -> None:
        pool = CodexPoolStatus(
            True,
            True,
            (
                account(1, active=False, remaining=0, hint="alt…"),
                account(2, active=True, remaining=90, hint="acc…"),
            ),
            2,
            1,
        )
        self.assertEqual(
            format_codex_rotation_event(
                pool,
                CodexRotationObservation(1, 2, 1, 1),
            ),
            "Codex quota exhausted for alt…; switched to acc…. "
            "Replacement status: available; 5h 90%, week 50%.",
        )

    def test_detects_account_switch_even_before_provider_429(self) -> None:
        pool = CodexPoolStatus(
            True,
            True,
            (
                account(1, active=False, remaining=5, hint="alt…"),
                account(2, active=True, remaining=90, hint="acc…"),
            ),
            2,
            0,
        )
        observation = detect_codex_rotation(
            CodexRuntimeSnapshot(4, 2, 2),
            pool=pool,
            previous_rate_limits=4,
            previous_rotations=2,
            previous_account_index=1,
        )
        self.assertEqual(observation, CodexRotationObservation(1, 2, 0, 0))

    def test_runtime_counter_reset_is_rebaselined_without_false_event(self) -> None:
        observation = detect_codex_rotation(
            CodexRuntimeSnapshot(0, 0, 1),
            pool=CodexPoolStatus(
                True, True, (account(1, active=True, remaining=80, hint="acc…"),), 1, 0
            ),
            previous_rate_limits=9,
            previous_rotations=3,
            previous_account_index=1,
        )
        self.assertIsNone(observation)

    def test_ordinary_account_selection_change_is_not_a_quota_event(self) -> None:
        pool = CodexPoolStatus(
            True,
            True,
            (
                account(1, active=False, remaining=80, hint="alt…"),
                account(2, active=True, remaining=90, hint="acc…"),
            ),
            2,
            0,
        )
        observation = detect_codex_rotation(
            CodexRuntimeSnapshot(4, 2, 2),
            pool=pool,
            previous_rate_limits=4,
            previous_rotations=2,
            previous_account_index=1,
        )
        self.assertIsNone(observation)

    def test_work_topic_is_notified_only_when_exactly_one_codex_topic_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = HubState.open(Path(directory) / "state.db")
            first = state.observe_topic(
                project_id="hub", chat_id=-1001, thread_id=1, title="General"
            )
            state.activate_agent(first.topic_id, "codex", "gpt-5.6-sol", "high")
            self.assertEqual(
                codex_rotation_targets(state, (-1001, 41)),
                ((-1001, 41), (-1001, 1)),
            )
            second = state.observe_topic(
                project_id="alpha", chat_id=-1002, thread_id=7, title="Backend"
            )
            state.activate_agent(second.topic_id, "codex", "gpt-5.6-sol", "high")
            self.assertEqual(codex_rotation_targets(state, (-1001, 41)), ((-1001, 41),))
            state.close()


if __name__ == "__main__":
    unittest.main()
