from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.incident_impact import classify_alerts
from hermes_codex_router.incident_journal import (
    IncidentJournal,
    omitted_actionable,
    read_incident_snapshot,
)
from hermes_codex_router.incident_runbooks import IMPACTS, RUNBOOKS
from hermes_codex_router.operational_alert import OperationalAlert

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
# Written by the schema 1 writer (main before ADR 0084) with seed bytes(range(32)).
SCHEMA_ONE_JOURNAL = (
    '{"schema_version":1,"seed_binding":"b3ed5473b7b439294ed6a4588d6f69bf07d0c4e85d760905878b46'
    'a14b1e0211","updated_at":"2026-01-01T00:00:00+00:00","omitted_open_count":0,"omitted_resol'
    'ved_count":0,"episodes":[{"id":"eb9210dfce084fdb63810f453eea3b37c341b948a88de1cc5a5ac68df8'
    '41b5d2.be364e9c1b2b451bb0c84550c6fd624b","opened_at":"2026-01-01T00:00:00+00:00","code":"u'
    'nresolved_provider_outcome","severity":"warning","message":"Hub has 1 unresolved provider '
    'outcome(s); run the local indeterminate audit before deciding any next action.","last_seen'
    '_at":"2026-01-01T00:00:00+00:00","resolved_at":null}]}'
)
SCHEMA_ONE_ID = "eb9210dfce084fdb63810f453eea3b37c341b948a88de1cc5a5ac68df841b5d2.be364e9c1b2b451bb0c84550c6fd624b"


def alert(key: str, code: str, impact: str | None = None) -> OperationalAlert:
    return OperationalAlert(key, code, "error", "Example condition.", impact)


def workers(*items: tuple[str, str, str]) -> dict[str, object]:
    return {
        "provider_workers": [
            {"instance_id": instance, "agent_id": agent, "status": status}
            for instance, agent, status in items
        ]
    }


class ImpactClassTests(unittest.TestCase):
    def test_every_catalogue_entry_has_a_known_impact(self) -> None:
        self.assertTrue(all(entry.impact in IMPACTS for entry in RUNBOOKS.values()))

    def test_catalogue_defaults_separate_stopped_work_from_noise(self) -> None:
        cases = {
            "unresolved_provider_outcome": "blocking",
            "provider_work_stalled": "blocking",
            "controller_health_stale": "blocking",
            "controller_health_degraded": "degraded",
            "unknown_delivery": "awaiting_owner",
            "hermes_group_policy_incomplete": "degraded",
            "codex_context_bloat": "diagnostic",
            "never_seen_historical_code": "degraded",
        }
        result = classify_alerts(tuple(alert(code, code) for code in cases), None)
        self.assertEqual({item.code: item.impact for item in result}, cases)

    def test_failed_worker_blocks_only_when_its_agent_has_no_healthy_slot(self) -> None:
        health = workers(
            ("claude-worker-1", "claude", "healthy"),
            ("claude-worker-3", "claude", "degraded"),
            ("opencode-worker-1", "opencode", "stale"),
        )
        result = classify_alerts(
            (
                alert("runtime:provider_worker:claude-worker-3", "provider_worker_health_degraded"),
                alert("runtime:provider_worker:opencode-worker-1", "provider_worker_health_stale"),
            ),
            health,
        )
        self.assertEqual([item.impact for item in result], ["degraded", "blocking"])

    def test_long_and_colliding_instance_ids_follow_the_evaluator_key(self) -> None:
        from hermes_codex_router.alerts import evaluate_operational_alerts

        long_id = "x" * 129
        for health, expected in (
            (workers((long_id, "solo", "stale")), "blocking"),
            (
                workers((long_id, "solo", "stale"), ("x" * 128 + "y", "other", "healthy")),
                "blocking",
            ),
            (workers((long_id, "pair", "stale"), ("pair-2", "pair", "healthy")), "degraded"),
        ):
            with self.subTest(expected=expected):
                evaluated = evaluate_operational_alerts(
                    state_snapshot={},
                    doctor_ok=True,
                    runtime_health={
                        "provider_workers": [
                            dict(item, component="provider_worker")
                            for item in health["provider_workers"]
                            if item["status"] != "healthy"
                        ]
                    },
                )
                result = classify_alerts(evaluated, health)
                self.assertEqual([item.impact for item in result], [expected])

    def test_explicit_impact_from_the_evaluator_is_kept(self) -> None:
        result = classify_alerts((alert("example", "codex_context_bloat", "blocking"),), None)
        self.assertEqual(result[0].impact, "blocking")


class BlockerFirstJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "incident-journal.json"

    def project(self, alerts):
        with IncidentJournal(self.path).cycle() as journal:
            return journal.project(classify_alerts(tuple(alerts), None), now=NOW)

    def test_overflow_keeps_blockers_and_counts_omissions_per_impact(self) -> None:
        noise = [alert(f"noise:{index}", "hermes_recovery_unavailable") for index in range(4)]
        blocker = alert("root:blocked", "unresolved_provider_outcome")
        with patch("hermes_codex_router.incident_journal.MAX_EPISODES", 2):
            snapshot = self.project([*noise, blocker])
        impacts = [row["impact"] for row in snapshot["episodes"]]
        self.assertEqual(impacts, ["blocking", "degraded"])
        self.assertEqual(snapshot["omitted_open_by_impact"], {"degraded": 3})
        self.assertEqual(omitted_actionable(snapshot), 0)
        self.assertEqual(json.loads(self.path.read_text())["schema_version"], 2)

    def test_omitted_actionable_episode_is_reported(self) -> None:
        blockers = [alert(f"root:{index}", "provider_work_stalled") for index in range(3)]
        with patch("hermes_codex_router.incident_journal.MAX_EPISODES", 2):
            snapshot = self.project(blockers)
        self.assertEqual(omitted_actionable(snapshot), 1)

    def test_frozen_schema_one_journal_keeps_identity_and_shared_reader(self) -> None:
        self.path.parent.chmod(0o700)
        seed = self.path.with_suffix(".seed")
        seed.write_bytes(bytes(range(32)))
        seed.chmod(0o600)
        self.path.write_text(SCHEMA_ONE_JOURNAL)
        self.path.chmod(0o600)
        legacy = read_incident_snapshot(self.path)
        self.assertEqual(legacy["episodes"][0]["impact"], "blocking")
        key = "reliability:unresolved-provider-outcome"
        later = NOW.replace(minute=5)
        with IncidentJournal(self.path).cycle() as journal:
            upgraded = journal.project(
                classify_alerts((alert(key, "unresolved_provider_outcome"),), None), now=later
            )
        row = upgraded["episodes"][0]
        self.assertEqual((row["id"], row["opened_at"]), (SCHEMA_ONE_ID, NOW.isoformat()))
        self.assertEqual(row["impact"], "blocking")
        self.assertEqual(read_incident_snapshot(self.path)["schema_version"], 2)


if __name__ == "__main__":
    unittest.main()
