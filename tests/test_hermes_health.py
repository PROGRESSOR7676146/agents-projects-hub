from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_codex_router.hermes_health import (
    probe_gateway_heartbeat,
    probe_hermes_bot_api,
    probe_hermes_group_policy,
    restart_hermes_gateway,
    sync_hermes_group_policy,
)


class HermesHealthTests(unittest.TestCase):
    def test_group_policy_reports_missing_project_chats(self) -> None:
        values = {
            "platforms.telegram.allowed_chats": [-1001],
            "platforms.telegram.group_allowed_chats": [-1001, -1002],
        }

        def run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(values[argv[3]]), "")

        result = probe_hermes_group_policy((-1001, -1002), run=run)

        self.assertFalse(result.ok)
        self.assertEqual(result.missing_allowed_chats, (-1002,))
        self.assertEqual(result.missing_group_allowed_chats, ())

    def test_policy_sync_merges_without_removing_other_hermes_groups(self) -> None:
        current = {
            "platforms.telegram.allowed_chats": [-9000, -1001],
            "platforms.telegram.group_allowed_chats": [-9000, -1001],
        }
        calls: list[tuple[str, ...]] = []

        def run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv[2] == "get":
                return subprocess.CompletedProcess(argv, 0, json.dumps(current[argv[3]]), "")
            return subprocess.CompletedProcess(argv, 0, "", "")

        changed = sync_hermes_group_policy((-1001, -1002), run=run)

        self.assertTrue(changed)
        writes = [call for call in calls if call[2] == "set"]
        self.assertEqual(len(writes), 2)
        self.assertEqual(json.loads(writes[0][4]), [-9000, -1002, -1001])

    def test_fresh_gateway_heartbeat_requires_live_socket_tick(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            heartbeat = Path(tempdir) / "gateway.heartbeat"
            now = datetime(2026, 8, 29, 18, 0, tzinfo=timezone.utc)
            heartbeat.write_text(
                json.dumps(
                    {
                        "updated_at": (now - timedelta(seconds=30)).isoformat(),
                        "loop_tick_socket": True,
                    }
                ),
                encoding="utf-8",
            )
            healthy = probe_gateway_heartbeat(heartbeat, now=now)
            heartbeat.write_text(
                json.dumps(
                    {
                        "updated_at": (now - timedelta(minutes=10)).isoformat(),
                        "loop_tick_socket": True,
                    }
                ),
                encoding="utf-8",
            )
            stale = probe_gateway_heartbeat(heartbeat, now=now)

        self.assertTrue(healthy.ok)
        self.assertFalse(stale.ok)

    def test_gateway_restart_uses_fixed_systemctl_argv(self) -> None:
        calls: list[tuple[str, ...]] = []

        def run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        restart_hermes_gateway("hermes-gateway.service", run=run)

        self.assertEqual(
            calls,
            [("systemctl", "--user", "restart", "hermes-gateway.service")],
        )

    def test_bot_api_probe_reads_private_env_and_reports_queue(self) -> None:
        class Api:
            def call(self, method: str) -> dict[str, int]:
                self_method = method
                self.assert_method(self_method)
                return {"pending_update_count": 2}

            @staticmethod
            def assert_method(method: str) -> None:
                if method != "getWebhookInfo":
                    raise AssertionError(method)

        with tempfile.TemporaryDirectory() as tempdir:
            env_path = Path(tempdir) / ".env"
            env_path.write_text("TELEGRAM_BOT_TOKEN=123:test-token\n", encoding="utf-8")
            env_path.chmod(0o600)
            result = probe_hermes_bot_api(env_path, api_factory=lambda _token: Api())

        self.assertTrue(result.ok)
        self.assertEqual(result.pending_updates, 2)


if __name__ == "__main__":
    unittest.main()
