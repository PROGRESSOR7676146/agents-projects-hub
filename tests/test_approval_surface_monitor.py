from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import Mock, patch

from hermes_codex_router import monitoring
from hermes_codex_router.approval_observations import identity_digest
from hermes_codex_router.approval_surface import ApprovalWait
from hermes_codex_router.state import HubState
from tests import test_incident_monitor as fixtures
from tests import test_preacceptance_approvals as early_fixtures


class ApprovalSurfaceMonitorTests(unittest.TestCase):
    def test_real_early_wait_is_observed_by_monitor_with_configured_profile(self):
        early = early_fixtures.PreacceptanceApprovalTests()
        early.setUp()
        self.addCleanup(early.doCleanups)
        early.observe(early.scope())
        f = fixtures.IncidentMonitorTests()
        cfg = f.config(str(early.root))
        cfg = replace(
            cfg,
            recovery_plane=replace(
                cfg.recovery_plane,
                enabled=True,
                tlive_config_path=early.root / "tlive/config.json",
            ),
        )
        sender = Mock()
        with (
            f.monitor(alerts=()),
            patch.object(monitoring, "datetime", wraps=datetime) as clock,
            patch.object(monitoring, "_hermes_health", return_value=None),
            patch.object(monitoring, "read_snapshot", return_value=None),
            patch.object(monitoring, "_operational_telegram", return_value=(sender, "hub")),
        ):
            clock.now.return_value = early.now + timedelta(seconds=60)
            result = monitoring.run_monitor_once(cfg, notify=True)
        surface = result["approval_surface"]
        assert isinstance(surface, dict)
        self.assertEqual(surface["waiting"], 1)
        self.assertEqual(surface["unknown"], 1)
        sender.send_html.assert_called_once()

    def test_unknown_deduplicates_and_recovery_rearms_without_native_calls(self):
        f = fixtures.IncidentMonitorTests()
        with tempfile.TemporaryDirectory() as directory:
            cfg = f.config(directory)
            cfg = replace(
                cfg,
                recovery_plane=replace(
                    cfg.recovery_plane,
                    enabled=True,
                    tlive_config_path=Path(directory) / "tlive/config.json",
                ),
            )
            now = datetime.now(timezone.utc)
            wait = ApprovalWait(
                "thread",
                "turn",
                identity_digest(7),
                identity_digest("item"),
                now - timedelta(seconds=60),
            )
            ms = now.timestamp() * 1000
            snapshot: dict[str, Any] = dict(
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
            sender = Mock()
            with (
                f.monitor(alerts=()),
                patch.object(monitoring, "_hermes_health", return_value=None),
                patch.object(
                    HubState, "pending_codex_approvals", return_value=((wait,), False)
                ) as pending,
                patch.object(monitoring, "read_snapshot", return_value=None) as read,
                patch.object(monitoring, "_operational_telegram", return_value=(sender, "hub")),
            ):
                first = monitoring.run_monitor_once(cfg, notify=True)
                monitoring.run_monitor_once(cfg, notify=True)
                counts = first["approval_surface"]
                assert isinstance(counts, dict)
                self.assertEqual(counts["unknown"], 1)
                self.assertEqual(sender.send_html.call_count, 1)
                fresh = replace(wait, observed_at=datetime.now(timezone.utc))
                grace_snapshot = dict(
                    snapshot,
                    entries=[
                        dict(snapshot["entries"][0], delivery="pending", graceUntil=ms + 10000)
                    ],
                )
                for observation, evidence in (
                    ((fresh,), None),
                    ((), None),
                    ((wait,), grace_snapshot),
                ):
                    pending.return_value = (observation, False)
                    read.return_value = evidence
                    monitoring.run_monitor_once(cfg, notify=True)
                    pending.return_value = ((wait,), False)
                    read.return_value = None
                    monitoring.run_monitor_once(cfg, notify=True)
                    self.assertEqual(sender.send_html.call_count, 1)
                    journal = json.loads(
                        cfg.state_path.with_name("incident-journal.json").read_text()
                    )
                    self.assertEqual(len(journal["episodes"]), 1)
                    self.assertIsNone(journal["episodes"][0]["resolved_at"])
                read.return_value = snapshot
                pending.return_value = ((fresh,), False)
                healthy = monitoring.run_monitor_once(cfg, notify=True)
                counts = healthy["approval_surface"]
                assert isinstance(counts, dict)
                self.assertEqual(counts["sent"], 1)
                journal = json.loads(cfg.state_path.with_name("incident-journal.json").read_text())
                self.assertIsNotNone(journal["episodes"][0]["resolved_at"])
                snapshot["entries"][0]["delivery"] = "failed"
                monitoring.run_monitor_once(cfg, notify=True)
                self.assertEqual(sender.send_html.call_count, 2)
                journal = json.loads(cfg.state_path.with_name("incident-journal.json").read_text())
                self.assertEqual(len(journal["episodes"]), 2)
