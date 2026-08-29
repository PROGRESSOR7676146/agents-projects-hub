from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.codex_accounts import CodexAccountStatus, CodexPoolStatus
from hermes_codex_router.provider_events import (
    codex_rotation_targets,
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
            format_codex_rotation_event(pool, 1),
            "Codex quota exhausted for alt…; rotated to acc….",
        )

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
