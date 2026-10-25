from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from hermes_codex_router import monitoring
from hermes_codex_router.approval_observations import identity_digest
from hermes_codex_router.approval_surface import ApprovalWait
from hermes_codex_router.state import HubState
from tests import test_incident_monitor as fixtures


class ApprovalSurfaceMonitorTests(unittest.TestCase):
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
            snapshot = dict(
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
                patch.object(HubState, "pending_codex_approvals", return_value=((wait,), False)),
                patch.object(monitoring, "read_snapshot", return_value=None) as read,
                patch.object(monitoring, "_operational_telegram", return_value=(sender, "hub")),
            ):
                first = monitoring.run_monitor_once(cfg, notify=True)
                monitoring.run_monitor_once(cfg, notify=True)
                counts = first["approval_surface"]
                assert isinstance(counts, dict)
                self.assertEqual(counts["unknown"], 1)
                self.assertEqual(sender.send_html.call_count, 1)
                read.return_value = snapshot
                healthy = monitoring.run_monitor_once(cfg, notify=True)
                counts = healthy["approval_surface"]
                assert isinstance(counts, dict)
                self.assertEqual(counts["sent"], 1)
                read.return_value = None
                monitoring.run_monitor_once(cfg, notify=True)
                self.assertEqual(sender.send_html.call_count, 2)
