from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from hermes_codex_router.approval_observations import identity_digest
from hermes_codex_router.approval_surface import ApprovalWait, assess_surface, surface_alerts


class ApprovalSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.wait = ApprovalWait(
            "thread",
            "turn",
            identity_digest(7),
            identity_digest("item"),
            self.now - timedelta(seconds=60),
        )
        ms = self.now.timestamp() * 1000
        self.snapshot: dict[str, Any] = dict(
            kind="approval.snapshot",
            version=1,
            epoch="00000000-0000-4000-8000-000000000001",
            epochStartedAt=ms - 120000,
            capturedAt=ms,
            truncated=False,
            entries=[
                dict(
                    threadId="thread",
                    turnId="turn",
                    providerRequestId=7,
                    itemId="item",
                    observedAt=ms - 60000,
                    deliveryAt=ms - 50000,
                    delivery="sent",
                )
            ],
        )

    def assess(self):
        return assess_surface([self.wait], self.snapshot, now=self.now)

    def test_exact_delivery_receipt_projection(self):
        self.assertEqual(self.assess()["sent"], 1)
        self.assertEqual(surface_alerts(self.assess()), ())

    def test_lifecycle_notice_receipt_is_irrelevant(self):
        self.snapshot["entries"] = []
        self.snapshot["approval_wait_notice_delivered"] = True
        self.assertEqual(self.assess()["unknown"], 1)
        self.assertEqual(len(surface_alerts(self.assess())), 1)

    def test_missing_stale_future_restarted_truncated_are_unknown(self):
        for key, value in [
            ("capturedAt", self.snapshot["capturedAt"] - 6000),
            ("capturedAt", self.snapshot["capturedAt"] + 1),
            ("truncated", True),
            ("epochStartedAt", self.snapshot["capturedAt"]),
        ]:
            with self.subTest(key=key):
                original = self.snapshot[key]
                self.snapshot[key] = value
                self.assertEqual(self.assess()["unknown"], 1)
                self.snapshot[key] = original
        self.assertEqual(assess_surface([self.wait], None, now=self.now)["unknown"], 1)

    def test_exact_thread_turn_item_and_typed_request(self):
        for key, value in [
            ("threadId", "other"),
            ("turnId", "other"),
            ("itemId", "other"),
            ("providerRequestId", "7"),
            ("providerRequestId", True),
        ]:
            with self.subTest(key=key):
                e = self.snapshot["entries"][0]
                original = e[key]
                e[key] = value
                self.assertEqual(self.assess()["unknown"], 1)
                e[key] = original

    def test_grace_never_claims_health_or_alerts(self):
        wait = ApprovalWait("thread", "turn", identity_digest(7), identity_digest("item"), self.now)
        counts = assess_surface([wait], None, now=self.now)
        self.assertEqual(counts["grace"], 1)
        self.assertEqual(counts["sent"], 0)
        self.assertEqual(surface_alerts(counts), ())

    def test_pending_sending_failed_unknown_and_duplicate(self):
        e = self.snapshot["entries"][0]
        for state in ["pending", "sending", "failed", "unknown"]:
            e["delivery"] = state
            self.assertEqual(self.assess()["sent"], 0)
            self.assertEqual(len(surface_alerts(self.assess())), 1)
        e["delivery"] = "sent"
        self.snapshot["entries"].append(dict(e))
        self.assertEqual(self.assess()["unknown"], 1)

    def test_malformed_values_remain_unknown(self):
        for value in ([], {}, True, 10**400):
            original = self.snapshot["entries"][0]["delivery"]
            self.snapshot["entries"][0]["delivery"] = value
            self.assertEqual(self.assess()["unknown"], 1)
            self.snapshot["entries"][0]["delivery"] = original
            original = self.snapshot["capturedAt"]
            self.snapshot["capturedAt"] = value
            self.assertEqual(self.assess()["unknown"], 1)
            self.snapshot["capturedAt"] = original

    def test_matched_configured_grace_does_not_alarm(self):
        e = self.snapshot["entries"][0]
        e["delivery"] = "pending"
        e["graceUntil"] = self.snapshot["capturedAt"] + 1000
        counts = self.assess()
        self.assertEqual(counts["grace"], 1)
        self.assertEqual(counts["sent"], 0)
        self.assertEqual(surface_alerts(counts), ())
