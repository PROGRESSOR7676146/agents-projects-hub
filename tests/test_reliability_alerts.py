from __future__ import annotations

import unittest

from hermes_codex_router.reliability_alerts import evaluate_reliability_alerts


class ReliabilityAlertTests(unittest.TestCase):
    def test_alerts_on_stale_queue_delivery_and_new_unresolved_work(self) -> None:
        alerts = evaluate_reliability_alerts(
            {
                "queued_work": 1,
                "pending_delivery": 1,
                "oldest_queue_age_seconds": 901,
                "oldest_delivery_age_seconds": 301,
                "unresolved_uncertain_execution": 1,
            }
        )

        self.assertEqual(
            {alert.code for alert in alerts},
            {
                "provider_queue_age_exceeded",
                "telegram_delivery_age_exceeded",
                "unresolved_provider_outcome",
            },
        )

    def test_threshold_boundaries_and_historical_resolved_work_are_quiet(self) -> None:
        alerts = evaluate_reliability_alerts(
            {
                "queued_work": 1,
                "pending_delivery": 1,
                "oldest_queue_age_seconds": 900,
                "oldest_delivery_age_seconds": 300,
                "uncertain_execution": 37,
                "unresolved_uncertain_execution": 0,
            }
        )

        self.assertEqual(alerts, ())

    def test_invalid_or_inapplicable_telemetry_is_ignored(self) -> None:
        self.assertEqual(evaluate_reliability_alerts({}), ())
        self.assertEqual(
            evaluate_reliability_alerts(
                {
                    "queued_work": 0,
                    "pending_delivery": 0,
                    "oldest_queue_age_seconds": 9999,
                    "oldest_delivery_age_seconds": 9999,
                    "unresolved_uncertain_execution": "unknown",
                }
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()
