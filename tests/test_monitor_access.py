from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from contextlib import ExitStack, closing, contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from hermes_codex_router import monitoring
from hermes_codex_router.alerts import evaluate_operational_alerts
from hermes_codex_router.catalog_refresh import CatalogRefreshResult
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    OperationalAlertSettings,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.runtime_health import MONITOR_INSTANCE_ID
from hermes_codex_router.state import HubState
from hermes_codex_router.telegram import TelegramError

CHAT = -1000000000001
KEY = ("opencode", "example-project")


class MonitorAccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        token = root / "provider.token"
        token.write_text("123456:example-token")
        token.chmod(0o600)
        self.config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=root / "projects.json",
            state_path=root / "state.db",
            codex_socket_path=root / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Example"),
            projects=(ProjectBinding(KEY[1], CHAT),),
            agents=(
                AgentDefinition(
                    KEY[0],
                    "Example",
                    "example_provider_bot",
                    "opencode",
                    token,
                    False,
                    False,
                    "example-model",
                    "medium",
                ),
            ),
            operational_alerts=OperationalAlertSettings(CHAT, 41),
        )
        HubState.open(self.config.state_path, codex_permission_profile=None).close()
        self.bot = Mock()

    @contextmanager
    def fixtures(self, outcome):
        api = Mock()
        if isinstance(outcome, TelegramError):
            api.call.side_effect = outcome
        else:
            api.call.return_value = outcome

        def health(state, config):
            return {
                "monitor": {
                    "component": "monitor",
                    "instance_id": MONITOR_INSTANCE_ID,
                    "status": state.runtime_health_status("monitor", MONITOR_INSTANCE_ID).status,
                }
            }

        with ExitStack() as stack:
            stack.enter_context(patch.object(monitoring, "TelegramBotApi", return_value=api))
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
            stack.enter_context(patch.object(monitoring, "_hermes_health", return_value=None))
            stack.enter_context(
                patch.object(monitoring, "project_runtime_health", side_effect=health)
            )
            stack.enter_context(
                patch.object(monitoring, "_operational_telegram", return_value=(self.bot, "hub"))
            )
            yield api

    def cycle(self, outcome, *, notify=True):
        with self.fixtures(outcome):
            return monitoring.run_monitor_once(self.config, notify=notify)

    def read_health(self):
        with closing(HubState.open_existing(self.config.state_path)) as state:
            result = state.get_runtime_health("monitor", MONITOR_INSTANCE_ID)
            assert result is not None
            return result

    def journal(self):
        return json.loads(self.config.state_path.with_name("incident-journal.json").read_text())

    def test_only_confirmed_group_rejections_mean_inaccessible(self) -> None:
        cases = [
            ("api_rejection", 400, False),
            ("api_rejection", 403, False),
            ("api_http", 403, None),
            ("api_http", 400, None),
            ("api_http", 502, None),
            ("api_rejection", 429, None),
            ("api_rejection", 401, None),
            ("network_timeout", None, None),
            ("network_dns", None, None),
            ("network_tls", None, None),
            ("network_io", None, None),
            ("unknown", None, None),
        ]
        for failure_class, status, expected in cases:
            with self.subTest(failure_class=failure_class, status=status):
                error = TelegramError(
                    "private-example-diagnostic",
                    operation="api_call",
                    failure_class=failure_class,
                    status_code=status,
                )
                with self.fixtures(error):
                    access = monitoring._telegram_access(self.config)
                self.assertIs(access[KEY], expected)

    def test_invalid_or_wrong_chat_response_does_not_prove_access(self) -> None:
        for response in (None, [], {}, {"id": CHAT + 1}, {"id": True}):
            with self.subTest(response=response), self.fixtures(response):
                self.assertIsNone(monitoring._telegram_access(self.config)[KEY])
        with self.fixtures({"id": CHAT}):
            self.assertTrue(monitoring._telegram_access(self.config)[KEY])

    def test_unknown_does_not_become_group_unavailable_alert(self) -> None:
        self.assertEqual(
            evaluate_operational_alerts(
                state_snapshot={}, doctor_ok=True, telegram_access={KEY: None}
            ),
            (),
        )

    def test_transient_failure_is_diagnostic_and_does_not_notify(self) -> None:
        result = self.cycle(
            TelegramError("private-example-diagnostic", failure_class="network_timeout")
        )
        self.assertEqual(result["alerts"], [])
        self.assertEqual(
            result["telegram_access"], {"available": 0, "unavailable": 0, "unknown": 1}
        )
        self.bot.send_html.assert_not_called()
        self.assertEqual(self.read_health().transport_consecutive_failures, 1)
        self.cycle({"id": CHAT})
        self.assertEqual(self.read_health().transport_consecutive_failures, 0)

    def test_three_failed_cycles_degrade_once_and_success_rearms(self) -> None:
        error = TelegramError(
            "private-example-diagnostic", operation="api_call", failure_class="network_timeout"
        )
        self.cycle(error)
        self.cycle(error)
        self.bot.send_html.assert_not_called()
        third = self.cycle(error)
        alerts = third["alerts"]
        assert isinstance(alerts, list)
        self.assertEqual([row["code"] for row in alerts], ["monitor_health_degraded"])
        self.assertEqual(self.read_health().transport_consecutive_failures, 3)
        self.cycle(error)
        self.bot.send_html.assert_called_once()
        self.cycle({"id": CHAT})
        for _ in range(3):
            self.cycle(error)
        self.assertEqual(self.bot.send_html.call_count, 2)
        with closing(HubState.open_existing(self.config.state_path)) as state:
            events = state.status_snapshot()["runtime_events"]
        assert isinstance(events, list)
        self.assertEqual(sum(row["code"] == "telegram_transport_error" for row in events), 2)
        self.assertEqual(sum(row["code"] == "telegram_recovered" for row in events), 1)
        self.assertNotIn("private-example-diagnostic", str(events) + str(self.read_health()))

    def test_unknown_preserves_episode_and_claim_until_confirmed_recovery(self) -> None:
        denied = TelegramError("example denial", failure_class="api_rejection", status_code=403)
        self.cycle(denied)
        original = self.journal()["episodes"][0]
        self.cycle(TelegramError("example timeout", failure_class="network_timeout"))
        self.assertEqual(self.journal()["episodes"][0], original)
        self.cycle(denied)
        self.bot.send_html.assert_called_once()
        self.assertEqual(self.journal()["episodes"][0]["id"], original["id"])
        self.cycle({"id": CHAT})
        self.assertIsNotNone(self.journal()["episodes"][0]["resolved_at"])
        self.cycle(denied)
        self.assertEqual(self.bot.send_html.call_count, 2)
        opened = [row for row in self.journal()["episodes"] if row["resolved_at"] is None]
        self.assertEqual(len(opened), 1)
        self.assertNotEqual(opened[0]["id"], original["id"])

    def test_unknown_does_not_fabricate_recovery_when_notify_is_disabled(self) -> None:
        self.cycle(
            TelegramError("example denial", failure_class="api_rejection", status_code=403),
            notify=False,
        )
        original = self.journal()["episodes"][0]
        self.cycle(TelegramError("example timeout", failure_class="network_timeout"), notify=False)
        self.assertEqual(self.journal()["episodes"][0], original)

    def test_non_telegram_http_denial_does_not_clear_transport_outage(self) -> None:
        for _ in range(3):
            self.cycle(TelegramError("example timeout", failure_class="network_timeout"))
        for status in (400, 403):
            result = self.cycle(
                TelegramError(
                    "example proxy response",
                    operation="api_call",
                    failure_class="api_http",
                    status_code=status,
                )
            )
            self.assertEqual(
                result["telegram_access"], {"available": 0, "unavailable": 0, "unknown": 1}
            )
        health = self.read_health()
        self.assertEqual(health.transport_consecutive_failures, 5)
        self.assertIsNone(health.transport_success_at)
        self.bot.send_html.assert_called_once()
        with closing(HubState.open_existing(self.config.state_path)) as state:
            events = state.status_snapshot()["runtime_events"]
        assert isinstance(events, list)
        self.assertFalse(any(row["code"] == "telegram_recovered" for row in events))

    def test_many_failed_probes_in_one_cycle_count_as_one_failure(self) -> None:
        self.config = replace(
            self.config,
            projects=tuple(ProjectBinding(f"example-{i}", CHAT - i) for i in range(5)),
        )
        result = self.cycle(TelegramError("example timeout", failure_class="network_timeout"))
        self.assertEqual(result["alerts"], [])
        self.assertEqual(self.read_health().transport_consecutive_failures, 1)
        self.assertEqual(
            result["telegram_access"], {"available": 0, "unavailable": 0, "unknown": 5}
        )

    def test_monitor_failure_preserves_unresolved_transport_streak(self) -> None:
        error = TelegramError("example timeout", failure_class="network_timeout")
        self.cycle(error)
        self.cycle(error)
        with (
            self.fixtures(error),
            patch.object(
                monitoring, "refresh_provider_catalogs", side_effect=RuntimeError("example")
            ),
            self.assertRaises(RuntimeError),
        ):
            monitoring.run_monitor_once(self.config, notify=False)
        self.assertEqual(self.read_health().transport_consecutive_failures, 2)
        self.cycle(error)
        self.bot.send_html.assert_called_once()

    def test_full_monitor_fallback_passes_body_and_same_operations_destination(self) -> None:
        self.config = replace(
            self.config, recovery_plane=replace(self.config.recovery_plane, enabled=True)
        )
        delivered = []
        original = monitoring._send_hermes

        def cli(argv, **kwargs):
            self.assertEqual(
                argv, ("hermes", "send", "--to", f"telegram:{CHAT}:41", "--quiet", "--file", "-")
            )
            delivered.append(kwargs["input"])
            return Mock(returncode=0)

        self.bot.send_html.side_effect = TelegramError("example send timeout")
        denied = TelegramError("example denied", failure_class="api_rejection", status_code=403)
        with (
            self.fixtures(denied),
            patch.object(
                monitoring,
                "run_doctor",
                return_value={
                    "ok": True,
                    "checks": [
                        {"name": "recovery:hermes", "ok": True},
                        {"name": "recovery:tlive", "ok": True},
                    ],
                },
            ),
            patch.object(
                monitoring,
                "_send_hermes",
                side_effect=lambda target, body: original(target, body, run=cli),
            ),
        ):
            result = monitoring.run_monitor_once(self.config, notify=True)
            monitoring.run_monitor_once(self.config, notify=True)
        self.assertEqual(result["delivered"], ["telegram_bot_group_unavailable:hermes-fallback"])
        self.assertEqual(len(delivered), 1)
        self.assertIn("Project Hub operational alert\n[WARNING]", delivered[0])
        self.assertIn("cannot access a registered project group", delivered[0])

    def test_hermes_stdin_contract_preserves_multiline_body(self) -> None:
        # Independent CLI contract: positional '-' is text; --file '-' reads stdin.
        parser = argparse.ArgumentParser()
        parser.add_argument("--to")
        parser.add_argument("--quiet", action="store_true")
        parser.add_argument("--file")
        parser.add_argument("message", nargs="?")
        delivered = []

        def cli(argv, **kwargs):
            args = parser.parse_args(argv[2:])
            delivered.append(kwargs["input"] if args.file == "-" else args.message)
            self.assertEqual(args.to, f"telegram:{CHAT}:41")
            return Mock(returncode=0)

        message = "Project Hub operational alert\n[WARNING] Связь временно недоступна."
        monitoring._send_hermes(f"telegram:{CHAT}:41", message, run=cli)
        self.assertEqual(delivered, [message])

    def test_hermes_cli_failure_is_not_reported_as_delivery(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "delivery failed"):
            monitoring._send_hermes(
                "telegram", "Example", run=Mock(return_value=Mock(returncode=1))
            )


if __name__ == "__main__":
    unittest.main()
