from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from hermes_codex_router.alerts import (
    _extract_latest_session_token_usage,
    check_codex_session_bloat,
    evaluate_operational_alerts,
)
from hermes_codex_router.codex_accounts import CodexAccountStatus, CodexPoolStatus
from hermes_codex_router.hub_config import OperationalAlertSettings
from hermes_codex_router.monitoring import (
    _claim_operational_alert,
    _destination,
    _release_recovered_quota_alerts,
    _release_resolved_operational_alerts,
    _send_hermes,
)
from hermes_codex_router.state import HubState


class OperationalAlertTests(unittest.TestCase):
    def test_reports_unavailable_low_quota_and_stuck_dispatch(self) -> None:
        pool = CodexPoolStatus(
            available=True,
            rotation_enabled=True,
            accounts=(
                CodexAccountStatus(
                    1,
                    True,
                    "ready",
                    "low",
                    5,
                    53,
                    None,
                    None,
                    None,
                    False,
                    "pr***@***.com",
                ),
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
            {"codex_5h_low", "dispatch_stuck"},
        )
        rendered = "\n".join(alert.message for alert in alerts)
        self.assertNotIn("dispatch-secret", rendered)
        self.assertIn("account 1 (pr***@***.com)", rendered)

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

    def test_optional_codex_pool_is_silent_when_not_configured(self) -> None:
        alerts = evaluate_operational_alerts(
            pool=CodexPoolStatus(False, False, (), None, 0, "not_configured"),
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
        )

        self.assertNotIn(
            "codex_pool_unavailable",
            {alert.code for alert in alerts},
        )

    def test_exhausted_inactive_account_is_status_not_alert_after_rotation(self) -> None:
        alerts = evaluate_operational_alerts(
            pool=CodexPoolStatus(
                True,
                True,
                (
                    CodexAccountStatus(
                        1, True, "ready", "low", 100, 84, None, None, 1, False, "abc…"
                    ),
                    CodexAccountStatus(
                        2, False, "unavailable", "high", 98, 0, None, None, 1, False, "xyz…"
                    ),
                ),
                1,
                0,
            ),
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
        )

        self.assertEqual(alerts, ())

    def test_inactive_unavailable_account_is_status_while_replacement_is_ready(self) -> None:
        alerts = evaluate_operational_alerts(
            pool=CodexPoolStatus(
                True,
                True,
                (
                    CodexAccountStatus(1, True, "ready", "low", 80, 80, None, None, 1, False),
                    CodexAccountStatus(
                        2, False, "unavailable", "high", 90, 90, None, None, 1, False
                    ),
                ),
                1,
                0,
            ),
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
        )

        self.assertEqual(alerts, ())

    def test_unavailable_account_alerts_when_no_replacement_is_ready(self) -> None:
        alerts = evaluate_operational_alerts(
            pool=CodexPoolStatus(
                True,
                True,
                (
                    CodexAccountStatus(
                        1, True, "unavailable", "high", 90, 90, None, None, 1, False
                    ),
                    CodexAccountStatus(
                        2, False, "unavailable", "high", 80, 80, None, None, 1, False
                    ),
                ),
                None,
                0,
            ),
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
        )

        self.assertEqual(
            [alert.code for alert in alerts],
            ["codex_account_unavailable", "codex_account_unavailable"],
        )
        self.assertTrue(all("quota and authentication" in alert.message for alert in alerts))

    def test_stale_quota_does_not_page_as_if_it_were_current(self) -> None:
        pool = CodexPoolStatus(
            available=True,
            rotation_enabled=True,
            accounts=(
                CodexAccountStatus(
                    1,
                    True,
                    "ready",
                    "low",
                    0,
                    1,
                    None,
                    None,
                    1,
                    True,
                    "ac…",
                ),
            ),
            recommended_account=1,
            account_rotations=0,
        )

        alerts = evaluate_operational_alerts(
            pool=pool,
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
        )

        self.assertEqual(alerts, ())

    def test_default_low_quota_band_starts_at_five_percent(self) -> None:
        def pool(remaining: int) -> CodexPoolStatus:
            return CodexPoolStatus(
                True,
                True,
                (CodexAccountStatus(1, True, "ready", "low", remaining, 80, None, None, 1, False),),
                1,
                0,
            )

        six = evaluate_operational_alerts(
            pool=pool(6), state_snapshot={"pending_dispatches": []}, doctor_ok=True
        )
        five = evaluate_operational_alerts(
            pool=pool(5), state_snapshot={"pending_dispatches": []}, doctor_ok=True
        )

        self.assertEqual(six, ())
        self.assertEqual([item.code for item in five], ["codex_5h_low"])

    def test_quota_warning_is_once_per_low_band_and_rearms_after_recovery(self) -> None:
        alert = evaluate_operational_alerts(
            pool=CodexPoolStatus(
                True,
                True,
                (CodexAccountStatus(1, True, "ready", "low", 5, 80, None, None, 1, False),),
                1,
                0,
            ),
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
        )[0]
        with TemporaryDirectory() as directory:
            state = HubState.open(Path(directory) / "state.db")
            self.assertTrue(_claim_operational_alert(state, alert, cooldown_seconds=0))
            self.assertFalse(_claim_operational_alert(state, alert, cooldown_seconds=0))
            _release_recovered_quota_alerts(
                state,
                CodexPoolStatus(
                    True,
                    True,
                    (CodexAccountStatus(1, True, "ready", "low", 80, 80, None, None, 2, False),),
                    1,
                    0,
                ),
            )
            self.assertTrue(_claim_operational_alert(state, alert, cooldown_seconds=0))
            state.close()

    def test_monitor_uses_only_configured_hub_operations_topic(self) -> None:
        settings = OperationalAlertSettings(-1000000000001, 41)
        self.assertEqual(_destination(settings), (-1000000000001, 41))

    def test_recovery_channels_are_reported_independently(self) -> None:
        pool = CodexPoolStatus(
            available=True,
            rotation_enabled=True,
            accounts=(),
            recommended_account=None,
            account_rotations=0,
        )
        one_down = evaluate_operational_alerts(
            pool=pool,
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
            recovery_status={"hermes": True, "tlive": False},
        )
        self.assertEqual({item.code for item in one_down}, {"tlive_recovery_unavailable"})
        self.assertEqual(one_down[0].severity, "warning")

        both_down = evaluate_operational_alerts(
            pool=pool,
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
            recovery_status={"hermes": False, "tlive": False},
        )
        self.assertEqual(
            {item.code for item in both_down},
            {
                "hermes_recovery_unavailable",
                "tlive_recovery_unavailable",
                "recovery_plane_unavailable",
            },
        )
        self.assertIn("error", {item.severity for item in both_down})

    def test_hermes_delivery_uses_argv_and_no_shell(self) -> None:
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

        class Result:
            returncode = 0

        def run(argv: tuple[str, ...], **kwargs: object) -> Result:
            calls.append((argv, kwargs))
            return Result()

        _send_hermes("telegram", "safe alert", run=run)
        self.assertEqual(calls[0][0], ("hermes", "send", "--to", "telegram", "--quiet", "-"))
        self.assertEqual(calls[0][1]["input"], "safe alert")
        self.assertNotIn("shell", calls[0][1])

    def test_missing_bot_group_access_is_alerted_per_agent_and_project(self) -> None:
        pool = CodexPoolStatus(
            available=True,
            rotation_enabled=True,
            accounts=(),
            recommended_account=None,
            account_rotations=0,
        )
        alerts = evaluate_operational_alerts(
            pool=pool,
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
            telegram_access={
                ("codex", "alpha"): True,
                ("opencode", "alpha"): False,
                ("antigravity", "beta"): False,
            },
        )
        self.assertEqual(
            {(item.key, item.severity) for item in alerts},
            {
                ("telegram:opencode:alpha", "warning"),
                ("telegram:antigravity:beta", "warning"),
            },
        )

    def test_hermes_policy_and_transport_failures_are_distinct(self) -> None:
        pool = CodexPoolStatus(
            available=True,
            rotation_enabled=True,
            accounts=(),
            recommended_account=None,
            account_rotations=0,
        )
        alerts = evaluate_operational_alerts(
            pool=pool,
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
            hermes_telegram={
                "policy_ok": False,
                "heartbeat_ok": True,
                "api_ok": True,
                "pending_updates": 2,
            },
        )

        self.assertEqual(
            {item.code for item in alerts},
            {"hermes_group_policy_incomplete", "hermes_telegram_updates_pending"},
        )

    def test_runtime_health_alerts_distinguish_each_expected_component(self) -> None:
        pool = CodexPoolStatus(True, True, (), None, 0)
        alerts = evaluate_operational_alerts(
            pool=pool,
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
            runtime_health={
                "controller": {
                    "component": "controller",
                    "instance_id": "project-hub-controller",
                    "status": "unknown",
                },
                "sender": {
                    "component": "sender",
                    "instance_id": "telegram-outbox-sender",
                    "status": "stale",
                },
                "provider_workers": [
                    {
                        "component": "provider_worker",
                        "instance_id": "codex-worker",
                        "agent_id": "codex",
                        "status": "degraded",
                    },
                    {
                        "component": "provider_worker",
                        "instance_id": "opencode-worker",
                        "agent_id": "opencode",
                        "status": "unknown",
                    },
                ],
            },
        )

        self.assertEqual(
            {item.code for item in alerts},
            {
                "controller_health_unknown",
                "sender_health_stale",
                "provider_worker_health_degraded",
                "provider_worker_health_unknown",
            },
        )
        self.assertEqual(len(alerts), 4)
        self.assertEqual(
            {item.key for item in alerts},
            {
                "runtime:controller:project-hub-controller",
                "runtime:sender:telegram-outbox-sender",
                "runtime:provider_worker:codex-worker",
                "runtime:provider_worker:opencode-worker",
            },
        )

        transitioned = evaluate_operational_alerts(
            pool=pool,
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
            runtime_health={
                "controller": {
                    "component": "controller",
                    "instance_id": "project-hub-controller",
                    "status": "stale",
                }
            },
        )
        self.assertEqual(transitioned[0].key, "runtime:controller:project-hub-controller")

    def test_deployment_revision_alert_is_one_per_episode_and_rearms(self) -> None:
        pool = CodexPoolStatus(True, True, (), None, 0)

        def alerts(status: str):
            return evaluate_operational_alerts(
                pool=pool,
                state_snapshot={"pending_dispatches": []},
                doctor_ok=True,
                runtime_health={"deployment_revision": {"status": status}},
            )

        unknown = alerts("unknown")
        mixed = alerts("mixed")
        self.assertEqual([item.code for item in unknown], ["deployment_revision_unknown"])
        self.assertEqual([item.key for item in mixed], ["deployment:revision"])
        with TemporaryDirectory() as directory:
            state = HubState.open(Path(directory) / "state.db")
            self.assertTrue(_claim_operational_alert(state, unknown[0], cooldown_seconds=0))
            self.assertFalse(_claim_operational_alert(state, mixed[0], cooldown_seconds=0))
            _release_resolved_operational_alerts(state, alerts("converged"))
            self.assertTrue(_claim_operational_alert(state, mixed[0], cooldown_seconds=0))
            state.close()

    def test_invalid_token_account_alerts_even_if_replacement_is_ready(self) -> None:
        pool = CodexPoolStatus(
            available=True,
            rotation_enabled=True,
            accounts=(
                CodexAccountStatus(
                    1, True, "ready", "low", 100, 100, None, None, None, False, "acc-1"
                ),
                CodexAccountStatus(
                    2,
                    False,
                    "unavailable",
                    "high",
                    None,
                    None,
                    None,
                    None,
                    None,
                    False,
                    "acc-2",
                    auth_invalidated=True,
                ),
            ),
            recommended_account=1,
            account_rotations=0,
        )
        alerts = evaluate_operational_alerts(
            pool=pool,
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
        )
        self.assertEqual({alert.code for alert in alerts}, {"codex_account_token_invalid"})
        self.assertIn("device-auth", alerts[0].message)

    def test_unreachable_configured_codex_proxy_alert_is_edge_triggered(self) -> None:
        pool = CodexPoolStatus(True, True, (), None, 0)
        alerts = evaluate_operational_alerts(
            pool=pool,
            state_snapshot={"pending_dispatches": []},
            doctor_ok=True,
            codex_config_proxy_ok=False,
        )
        self.assertEqual([alert.code for alert in alerts], ["codex_config_proxy_unavailable"])
        with TemporaryDirectory() as directory:
            state = HubState.open(Path(directory) / "state.db")
            self.assertTrue(_claim_operational_alert(state, alerts[0], cooldown_seconds=0))
            self.assertFalse(_claim_operational_alert(state, alerts[0], cooldown_seconds=0))
            _release_resolved_operational_alerts(
                state,
                evaluate_operational_alerts(
                    pool=pool,
                    state_snapshot={"pending_dispatches": []},
                    doctor_ok=True,
                    codex_config_proxy_ok=True,
                ),
            )
            self.assertTrue(_claim_operational_alert(state, alerts[0], cooldown_seconds=0))
            state.close()

    def test_extract_latest_session_token_usage(self) -> None:
        with TemporaryDirectory() as directory:
            p = Path(directory) / "rollout-test.jsonl"
            self.assertIsNone(_extract_latest_session_token_usage(p))

            p.write_text('{"type":"session_meta"}\n{"type":"turn_context"}\n', encoding="utf-8")
            self.assertIsNone(_extract_latest_session_token_usage(p))

            p.write_text(
                '{"type":"session_meta"}\n'
                '{"type":"token_usage_record","payload":{"session_id":"sess-123","usage":{"input_tokens":75000,"total_tokens":75500}}}\n'
                '{"type":"event_msg","payload":{"type":"task_complete"}}\n',
                encoding="utf-8",
            )
            extracted = _extract_latest_session_token_usage(p)
            self.assertIsNotNone(extracted)
            assert extracted is not None
            session_id, input_tokens, total_tokens = extracted
            self.assertEqual(session_id, "sess-123")
            self.assertEqual(input_tokens, 75000)
            self.assertEqual(total_tokens, 75500)

    def test_check_codex_session_bloat_alerts_and_ignores(self) -> None:
        with TemporaryDirectory() as directory:
            sessions_dir = Path(directory)
            now = datetime(2026, 9, 6, 4, 0, 0, tzinfo=timezone.utc)
            now_ts = now.timestamp()

            # Session 1: Bloated (75k tokens, recent)
            s1 = sessions_dir / "2026" / "09" / "06" / "rollout-2026-09-06-sess-bloat.jsonl"
            s1.parent.mkdir(parents=True, exist_ok=True)
            s1.write_text(
                '{"type":"token_usage_record","payload":{"session_id":"01a07027-bloated-uuid","usage":{"input_tokens":75000,"total_tokens":75500}}}\n',
                encoding="utf-8",
            )
            import os

            os.utime(s1, (now_ts - 300, now_ts - 300))  # 5 min ago

            # Session 2: Safe tokens (25k tokens, recent)
            s2 = sessions_dir / "2026" / "09" / "06" / "rollout-2026-09-06-sess-safe.jsonl"
            s2.write_text(
                '{"type":"token_usage_record","payload":{"session_id":"01a073bf-safe-uuid","usage":{"input_tokens":25000,"total_tokens":25200}}}\n',
                encoding="utf-8",
            )
            os.utime(s2, (now_ts - 300, now_ts - 300))

            # Session 3: Bloated but stale (100k tokens, 5 hours ago > max_age_seconds)
            s3 = sessions_dir / "2026" / "09" / "05" / "rollout-2026-09-05-sess-stale.jsonl"
            s3.parent.mkdir(parents=True, exist_ok=True)
            s3.write_text(
                '{"type":"token_usage_record","payload":{"session_id":"01a06999-stale-uuid","usage":{"input_tokens":100000,"total_tokens":101000}}}\n',
                encoding="utf-8",
            )
            os.utime(s3, (now_ts - 18000, now_ts - 18000))

            alerts = check_codex_session_bloat(
                sessions_dir,
                threshold_tokens=65000,
                max_age_seconds=7200,
                now=now,
            )
            self.assertEqual(len(alerts), 1)
            alert = alerts[0]
            self.assertEqual(alert.code, "codex_context_bloat")
            self.assertEqual(alert.key, "codex:session:01a07027-bloated-uuid:bloat")
            self.assertEqual(alert.severity, "warning")
            self.assertIn("01a07027", alert.message)
            self.assertIn("75,000 tokens", alert.message)
            self.assertIn("/compact", alert.message)

            # Session-size inspection remains available as a local diagnostic,
            # but routine monitoring must not turn it into an operational alert.
            pool = CodexPoolStatus(True, True, (), None, 0)
            evaluated = evaluate_operational_alerts(
                pool=pool,
                state_snapshot={"pending_dispatches": []},
                doctor_ok=True,
                now=now,
            )
            self.assertNotIn("codex_context_bloat", {a.code for a in evaluated})

    def test_session_bloat_labels_telegram_and_cli_sessions(self) -> None:
        with TemporaryDirectory() as directory:
            codex_home = Path(directory)
            sessions_dir = codex_home / "sessions"
            now = datetime(2026, 9, 6, 11, 0, 0, tzinfo=timezone.utc)
            now_ts = now.timestamp()

            # Session 1: Telegram session (mapped in state_snapshot)
            s1_id = "01a049a1-tg-uuid"
            s1 = sessions_dir / "2026" / "09" / "06" / f"rollout-{s1_id}.jsonl"
            s1.parent.mkdir(parents=True, exist_ok=True)
            s1.write_text(
                '{"type":"session_meta","payload":{"cwd":"/home/example/projects/example-project"}}\n'
                f'{{"type":"token_usage_record","payload":{{"session_id":"{s1_id}","usage":{{"input_tokens":80000,"total_tokens":81000}}}}}}\n',
                encoding="utf-8",
            )
            import os

            os.utime(s1, (now_ts - 60, now_ts - 60))

            # Session 2: CLI session with thread_name in session_index.jsonl
            s2_id = "01a0759e-cli-uuid"
            s2 = sessions_dir / "2026" / "09" / "06" / f"rollout-{s2_id}.jsonl"
            s2.write_text(
                '{"type":"session_meta","payload":{"cwd":"/home/example/projects/example-cli"}}\n'
                f'{{"type":"token_usage_record","payload":{{"session_id":"{s2_id}","usage":{{"input_tokens":95000,"total_tokens":96000}}}}}}\n',
                encoding="utf-8",
            )
            os.utime(s2, (now_ts - 60, now_ts - 60))

            idx_file = codex_home / "session_index.jsonl"
            idx_file.write_text(
                f'{{"id":"{s2_id}","thread_name":"Example CLI task","updated_at":"2026-09-06T11:00:00Z"}}\n',
                encoding="utf-8",
            )

            state_snapshot = {
                "topics": [
                    {
                        "topic_id": 1,
                        "project_id": "example-project",
                        "thread_id": 77,
                        "title": "Example topic",
                        "provider_session_id": s1_id,
                    }
                ],
                "pending_dispatches": [],
            }

            alerts = check_codex_session_bloat(
                sessions_dir,
                threshold_tokens=65000,
                max_age_seconds=7200,
                now=now,
                state_snapshot=state_snapshot,
            )
            self.assertEqual(len(alerts), 2)
            alert_by_key = {a.key: a for a in alerts}

            tg_alert = alert_by_key[f"codex:session:{s1_id}:bloat"]
            self.assertIn("Telegram [example-project: Example topic #77]", tg_alert.message)
            self.assertIn("80,000 tokens", tg_alert.message)

            cli_alert = alert_by_key[f"codex:session:{s2_id}:bloat"]
            self.assertIn('CLI "Example CLI task"', cli_alert.message)
            self.assertIn("95,000 tokens", cli_alert.message)


if __name__ == "__main__":
    unittest.main()
