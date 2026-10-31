from __future__ import annotations

import ast
import json
import tempfile
import unittest
from contextlib import ExitStack, closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from hermes_codex_router import alerts as alert_module
from hermes_codex_router import approval_surface, monitoring, reliability_alerts
from hermes_codex_router.alerts import evaluate_operational_alerts
from hermes_codex_router.catalog_refresh import CatalogRefreshResult
from hermes_codex_router.hub_config import HubConfig, OperationalAlertSettings, TerminalSettings
from hermes_codex_router.incident_journal import IncidentJournal, IncidentJournalError
from hermes_codex_router.incident_runbooks import (
    RUNBOOKS,
    RUNTIME_COMPONENTS,
    RUNTIME_FAILURE_STATUSES,
    incident_runbook,
)
from hermes_codex_router.monitoring import run_monitor_once
from hermes_codex_router.operational_alert import OperationalAlert
from hermes_codex_router.runtime_health import MONITOR_INSTANCE_ID
from hermes_codex_router.state import HubState

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class IncidentMonitorTests(unittest.TestCase):
    def config(self, directory: str) -> HubConfig:
        root = Path(directory)
        config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=root / "projects.json",
            state_path=root / "state.db",
            codex_socket_path=root / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(),
            agents=(),
            operational_alerts=OperationalAlertSettings(-1001234567890, 77),
        )
        HubState.open(config.state_path, codex_permission_profile=None).close()
        return config

    @contextmanager
    def monitor(self, *, alerts=None, stale=()):
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    monitoring,
                    "refresh_provider_catalogs",
                    return_value=CatalogRefreshResult((), (), {}),
                )
            )
            stack.enter_context(
                patch.object(monitoring, "run_doctor", return_value={"ok": True, "checks": []})
            )
            stack.enter_context(patch.object(monitoring, "_telegram_access", return_value={}))
            stack.enter_context(patch.object(monitoring, "project_runtime_health", return_value={}))
            stack.enter_context(
                patch.object(monitoring.ProviderCatalogCache, "stale_agents", return_value=stale)
            )
            if alerts is not None:
                stack.enter_context(
                    patch.object(monitoring, "evaluate_operational_alerts", return_value=alerts)
                )
            yield

    def test_projection_precedes_operations_and_does_not_mean_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            path = config.state_path.with_name("incident-journal.json")
            alert = OperationalAlert(
                "example-key",
                "deployment_unhealthy",
                "error",
                "Local diagnostics require attention.",
            )
            bot = Mock()

            def send(*args):
                row = json.loads(path.read_text())["episodes"][0]
                self.assertEqual(row["message"], alert.message)
                self.assertIn(alert.message, args[2])

            bot.send_html.side_effect = send
            with (
                self.monitor(alerts=(alert,)),
                patch.object(monitoring, "_operational_telegram", return_value=(bot, "hub")),
            ):
                first = run_monitor_once(config, notify=False)
                bot.send_html.assert_not_called()
                journal_status = first["incident_journal"]
                assert isinstance(journal_status, dict)
                self.assertEqual(journal_status["status"], "updated")
                run_monitor_once(config, notify=True)
                run_monitor_once(config, notify=True)
            bot.send_html.assert_called_once()
            with self.monitor(alerts=()):
                run_monitor_once(config, notify=False)
            self.assertIsNotNone(json.loads(path.read_text())["episodes"][0]["resolved_at"])

    def test_omitted_blocker_makes_the_monitor_visibly_saturated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            alerts = tuple(
                OperationalAlert(f"stalled:{index}", "provider_work_stalled", "error", "Stalled")
                for index in range(3)
            )
            with (
                self.monitor(alerts=alerts),
                patch("hermes_codex_router.incident_journal.MAX_EPISODES", 2),
            ):
                result = run_monitor_once(config, notify=False)
            self.assertFalse(result["ok"])
            self.assertEqual(result["incident_journal"]["status"], "saturated")
            self.assertEqual(result["incident_journal"]["omitted_open_by_impact"], {"blocking": 1})
            with closing(HubState.open_existing(config.state_path)) as state:
                health = state.get_runtime_health("monitor", MONITOR_INSTANCE_ID)
                assert health is not None
                self.assertEqual(health.error_code, "incident_journal_saturated")

    def test_projection_failure_preserves_operations_and_reports_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            bot = Mock()
            alert = OperationalAlert("example", "deployment_unhealthy", "error", "Example trigger")
            with (
                self.monitor(alerts=(alert,)),
                patch.object(IncidentJournal, "project", side_effect=IncidentJournalError()),
                patch.object(monitoring, "_operational_telegram", return_value=(bot, "hub")),
            ):
                result = run_monitor_once(config, notify=True)
            bot.send_html.assert_called_once()
            self.assertFalse(result["ok"])
            self.assertEqual(
                result["incident_journal"],
                {"status": "unavailable", "error_code": "incident_journal_unavailable"},
            )
            with closing(HubState.open_existing(config.state_path)) as state:
                health = state.get_runtime_health("monitor", MONITOR_INSTANCE_ID)
                assert health is not None
                self.assertEqual(health.error_code, "incident_journal_unavailable")
                self.assertIsNone(health.success_at)
            with self.monitor(alerts=()):
                recovered = run_monitor_once(config, notify=False)
            self.assertTrue(recovered["ok"])
            with closing(HubState.open_existing(config.state_path)) as state:
                health = state.get_runtime_health("monitor", MONITOR_INSTANCE_ID)
                assert health is not None
                self.assertIsNone(health.error_code)

    def test_busy_monitor_does_not_evaluate_or_publish_health(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            with (
                IncidentJournal(config.state_path.with_name("incident-journal.json")).cycle(),
                patch.object(monitoring, "run_doctor") as doctor,
                patch.object(HubState, "upsert_runtime_health") as publish,
            ):
                self.assertEqual(
                    run_monitor_once(config, notify=False),
                    {"ok": False, "error_code": "monitor_busy"},
                )
                doctor.assert_not_called()
                publish.assert_not_called()

    def test_identity_canaries_absent_from_all_live_messages_and_journal(self) -> None:
        canary = "example-private-identity-canary"
        evaluated = evaluate_operational_alerts(
            state_snapshot={
                "topics": [{"topic_id": 11, "project_id": canary, "title": canary}],
                "pending_dispatches": [
                    {
                        "topic_id": 11,
                        "agent_id": canary,
                        "updated_at": (NOW - timedelta(hours=1)).isoformat(),
                    }
                ],
            },
            doctor_ok=True,
            telegram_access={(canary, canary): False},
            runtime_health={
                "provider_workers": [
                    {
                        "component": "provider_worker",
                        "instance_id": canary,
                        "agent_id": canary,
                        "status": "stale",
                    },
                    {"component": canary, "instance_id": canary, "status": canary},
                ]
            },
            now=NOW,
        )
        self.assertTrue(any(canary in alert.key for alert in evaluated))
        self.assertTrue(all(canary not in alert.code + alert.message for alert in evaluated))
        with tempfile.TemporaryDirectory() as directory:
            config = self.config(directory)
            with self.monitor(alerts=evaluated, stale=(canary,)):
                result = run_monitor_once(config, notify=False)
            journal = config.state_path.with_name("incident-journal.json").read_text()
            self.assertNotIn(canary, journal)
            observed_alerts = result["alerts"]
            assert isinstance(observed_alerts, list)
            self.assertEqual(
                {row["message"] for row in json.loads(journal)["episodes"]},
                {row["message"] for row in observed_alerts},
            )

    def test_every_literal_emitted_code_and_runtime_variant_has_runbook(self) -> None:
        # Audit all constructor sites, including helpers not selected by a monitor cycle.
        # A new literal alert fails this contract even without a new scenario fixture.
        codes = set()
        for module in (alert_module, approval_surface, monitoring, reliability_alerts):
            module_file = module.__file__
            assert module_file is not None
            tree = ast.parse(Path(module_file).read_text())
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "OperationalAlert"
                ):
                    code = (
                        node.args[1]
                        if len(node.args) >= 2
                        else next(item.value for item in node.keywords if item.arg == "code")
                    )
                    if isinstance(code, ast.Constant):
                        codes.add(code.value)
        for component in RUNTIME_COMPONENTS:
            for status in RUNTIME_FAILURE_STATUSES:
                evaluated = evaluate_operational_alerts(
                    state_snapshot={},
                    doctor_ok=True,
                    runtime_health={
                        "provider_workers": [
                            {
                                "component": component,
                                "status": status,
                                "instance_id": "example-worker",
                            }
                        ]
                    },
                )
                self.assertEqual(len(evaluated), 1)
                codes.add(evaluated[0].code)
        for status in ("mixed", "unknown"):
            codes.update(
                alert.code
                for alert in evaluate_operational_alerts(
                    state_snapshot={},
                    doctor_ok=True,
                    runtime_health={"deployment_revision": {"status": status}},
                )
            )
        codes.update(
            alert.code
            for alert in reliability_alerts.evaluate_reliability_alerts(
                {
                    "stalled_provider_work": 1,
                    "pending_delivery": 1,
                    "oldest_delivery_age_seconds": 301,
                    "pending_progress_delivery": 1,
                    "oldest_progress_delivery_age_seconds": 301,
                    "unknown_delivery": 1,
                    "unknown_progress_delivery": 1,
                    "unresolved_uncertain_execution": 1,
                }
            )
        )
        self.assertEqual(codes, set(RUNBOOKS))
        for runbook in RUNBOOKS.values():
            self.assertGreaterEqual(len(runbook.options), 2)
            self.assertIn(
                runbook.recommended_option, {option.option_id for option in runbook.options}
            )
            self.assertTrue(all(option.consequence for option in runbook.options))

    def test_unknown_historical_code_has_generic_guidance(self) -> None:
        guidance = incident_runbook("example-unknown-sensitive-code")
        self.assertNotIn("example-unknown-sensitive-code", str(guidance))
        self.assertEqual(guidance.recommended_option, "inspect")


if __name__ == "__main__":
    unittest.main()
