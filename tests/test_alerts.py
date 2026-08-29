from __future__ import annotations

import unittest
from datetime import datetime, timezone

from hermes_codex_router.alerts import evaluate_operational_alerts
from hermes_codex_router.codex_accounts import CodexAccountStatus, CodexPoolStatus
from hermes_codex_router.monitoring import _destinations


class OperationalAlertTests(unittest.TestCase):
    def test_reports_unavailable_low_quota_and_stuck_dispatch(self) -> None:
        pool = CodexPoolStatus(
            available=True,
            rotation_enabled=True,
            accounts=(
                CodexAccountStatus(1, True, "ready", "low", 8, 53, None, None, None, False),
                CodexAccountStatus(
                    2, False, "unavailable", "high", None, 67, None, None, None, False
                ),
            ),
            recommended_account=1,
            account_rotations=0,
        )
        snapshot: dict[str, object] = {
            "pending_dispatches": [
                {
                    "dispatch_id": "dispatch-secret",
                    "topic_id": 7,
                    "agent_id": "codex",
                    "status": "running",
                    "created_at": "2026-08-29T06:00:00+00:00",
                    "updated_at": "2026-08-29T06:00:00+00:00",
                }
            ]
        }

        alerts = evaluate_operational_alerts(
            pool=pool,
            state_snapshot=snapshot,
            doctor_ok=True,
            now=datetime(2026, 8, 29, 6, 30, tzinfo=timezone.utc),
        )

        self.assertEqual(
            {alert.code for alert in alerts},
            {"codex_5h_low", "codex_account_unavailable", "dispatch_stuck"},
        )
        rendered = "\n".join(alert.message for alert in alerts)
        self.assertNotIn("dispatch-secret", rendered)

    def test_healthy_state_has_no_alerts(self) -> None:
        pool = CodexPoolStatus(
            available=True,
            rotation_enabled=True,
            accounts=(
                CodexAccountStatus(1, True, "ready", "low", 60, 70, None, None, None, False),
            ),
            recommended_account=1,
            account_rotations=2,
        )
        alerts = evaluate_operational_alerts(
            pool=pool,
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
            now=datetime(2026, 8, 29, 6, 30, tzinfo=timezone.utc),
        )
        self.assertEqual(alerts, ())

    def test_monitor_chooses_one_destination_per_chat(self) -> None:
        snapshot = {
            "topics": [
                {"chat_id": -1001, "thread_id": 7},
                {"chat_id": -1001, "thread_id": 8},
                {"chat_id": -1002, "thread_id": 3},
            ]
        }
        self.assertEqual(_destinations(snapshot), [(-1001, 7), (-1002, 3)])


if __name__ == "__main__":
    unittest.main()
