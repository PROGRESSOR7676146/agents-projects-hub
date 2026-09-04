from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.cli import main
from hermes_codex_router.hub_config import AgentDefinition, HubConfig, TerminalSettings
from hermes_codex_router.release_identity import ReleaseIdentity
from hermes_codex_router.runtime_health import project_runtime_health
from hermes_codex_router.state import HubState, StateError


class RuntimeHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state = HubState.open(Path(self.tempdir.name) / "state.db")
        self.now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def test_health_snapshot_round_trips_bounded_non_secret_fields(self) -> None:
        release = ReleaseIdentity("0.6.0", "a" * 40, "2026-09-04T12:00:00+00:00", True)
        record = self.state.upsert_runtime_health(
            component="provider_worker",
            instance_id="worker-opencode-1",
            runtime="opencode",
            agent_id="opencode",
            pid=1234,
            process_start_marker="linux-proc-start-42",
            started_at=self.now - timedelta(minutes=2),
            heartbeat_at=self.now,
            success_at=self.now - timedelta(seconds=5),
            error_code=None,
            active_job_id="job-example-1",
            active_lease_expires_at=self.now + timedelta(minutes=2),
            provider_state="ready",
            quota_remaining_percent=75.5,
            quota_reset_at=self.now + timedelta(hours=1),
            release_identity=release,
        )

        self.assertEqual(record.component, "provider_worker")
        self.assertEqual(record.instance_id, "worker-opencode-1")
        self.assertEqual(record.runtime, "opencode")
        self.assertEqual(record.agent_id, "opencode")
        self.assertEqual(record.pid, 1234)
        self.assertEqual(record.process_start_marker, "linux-proc-start-42")
        self.assertEqual(record.active_job_id, "job-example-1")
        self.assertEqual(record.provider_state, "ready")
        self.assertEqual(record.quota_remaining_percent, 75.5)
        self.assertEqual(record.release_version, "0.6.0")
        self.assertEqual(record.release_git_sha, "a" * 40)
        self.assertEqual(record.release_built_at, "2026-09-04T12:00:00+00:00")
        self.assertTrue(record.release_clean)
        self.assertEqual(
            self.state.get_runtime_health("provider_worker", "worker-opencode-1"),
            record,
        )

    def test_upsert_replaces_ephemeral_state_but_preserves_started_at(self) -> None:
        started = self.now - timedelta(minutes=2)
        self.state.upsert_runtime_health(
            component="sender",
            instance_id="sender-1",
            runtime="telegram",
            agent_id="opencode",
            pid=2345,
            process_start_marker="start-1",
            started_at=started,
            heartbeat_at=self.now - timedelta(seconds=5),
            error_code="telegram_timeout",
            active_job_id="outbox-example-1",
        )

        refreshed = self.state.upsert_runtime_health(
            component="sender",
            instance_id="sender-1",
            runtime="telegram",
            agent_id="opencode",
            pid=2345,
            process_start_marker="start-1",
            started_at=self.now,
            heartbeat_at=self.now,
            success_at=self.now,
            error_code=None,
            active_job_id=None,
        )

        self.assertEqual(refreshed.started_at, started.isoformat())
        self.assertIsNone(refreshed.error_code)
        self.assertIsNone(refreshed.active_job_id)
        self.assertEqual(refreshed.success_at, self.now.isoformat())

    def test_health_classification_uses_cached_timestamps_only(self) -> None:
        self.state.upsert_runtime_health(
            component="controller",
            instance_id="controller-healthy",
            pid=3456,
            process_start_marker="start-healthy",
            started_at=self.now - timedelta(minutes=5),
            heartbeat_at=self.now - timedelta(seconds=10),
            success_at=self.now - timedelta(seconds=20),
        )
        self.state.upsert_runtime_health(
            component="provider_worker",
            instance_id="worker-limited",
            runtime="codex",
            agent_id="codex",
            pid=4567,
            process_start_marker="start-limited",
            started_at=self.now - timedelta(minutes=5),
            heartbeat_at=self.now - timedelta(seconds=10),
            error_code="provider_limit",
            provider_state="limited",
            quota_remaining_percent=0,
        )
        self.state.upsert_runtime_health(
            component="sender",
            instance_id="sender-degraded",
            pid=5678,
            process_start_marker="start-degraded",
            started_at=self.now - timedelta(minutes=5),
            heartbeat_at=self.now - timedelta(seconds=90),
        )
        self.state.upsert_runtime_health(
            component="sender",
            instance_id="sender-transport-failed",
            pid=5679,
            process_start_marker="start-transport-failed",
            started_at=self.now - timedelta(minutes=5),
            heartbeat_at=self.now - timedelta(seconds=10),
            transport_operation="send_message",
            transport_failure_class="network_timeout",
            transport_consecutive_failures=2,
        )
        self.state.upsert_runtime_health(
            component="provider_worker",
            instance_id="worker-stale",
            runtime="antigravity",
            agent_id="antigravity",
            pid=6789,
            process_start_marker="start-stale",
            started_at=self.now - timedelta(minutes=10),
            heartbeat_at=self.now - timedelta(minutes=4),
        )

        def classify(component: str, instance: str) -> str:
            return self.state.runtime_health_status(
                component,
                instance,
                now=self.now,
                degraded_after=timedelta(seconds=60),
                stale_after=timedelta(minutes=3),
            ).status

        self.assertEqual(classify("controller", "controller-healthy"), "healthy")
        self.assertEqual(classify("provider_worker", "worker-limited"), "degraded")
        self.assertEqual(classify("sender", "sender-degraded"), "degraded")
        self.assertEqual(classify("sender", "sender-transport-failed"), "degraded")
        self.assertEqual(classify("provider_worker", "worker-stale"), "stale")
        self.assertEqual(classify("sender", "missing"), "unknown")

    def test_invalid_or_oversized_health_fields_fail_closed(self) -> None:
        common = dict(
            component="controller",
            instance_id="controller-1",
            pid=1234,
            process_start_marker="start-1",
            started_at=self.now,
            heartbeat_at=self.now,
        )
        with self.assertRaises(StateError):
            self.state.upsert_runtime_health(**cast(Any, common | {"error_code": "x" * 129}))
        with self.assertRaises(StateError):
            self.state.upsert_runtime_health(**cast(Any, common | {"quota_remaining_percent": 101}))
        with self.assertRaises(StateError):
            self.state.upsert_runtime_health(
                **cast(Any, common | {"provider_state": "secret-bearing-detail"})
            )
        with self.assertRaises(StateError):
            self.state.upsert_runtime_health(**cast(Any, common | {"pid": 0}))
        invalid_transport = (
            {"transport_operation": "poll-with-dash", "transport_consecutive_failures": 1},
            {"transport_failure_class": "network timeout", "transport_consecutive_failures": 1},
            {"transport_operation": "poll", "transport_consecutive_failures": 0},
            {"transport_operation": "poll", "transport_consecutive_failures": 1},
            {
                "transport_operation": "poll",
                "transport_failure_class": "network_timeout",
                "transport_status_code": 99,
                "transport_consecutive_failures": 1,
            },
            {
                "transport_operation": "poll",
                "transport_failure_class": "network_timeout",
                "transport_retry_after": 86_401,
                "transport_consecutive_failures": 1,
            },
        )
        for values in invalid_transport:
            with self.subTest(values=values), self.assertRaises(StateError):
                self.state.upsert_runtime_health(**cast(Any, common | values))

    def test_transport_health_round_trips_and_clears_after_recovery(self) -> None:
        failed = self.state.upsert_runtime_health(
            component="sender",
            instance_id="sender-transport",
            runtime="telegram",
            pid=1234,
            process_start_marker="sender-start",
            started_at=self.now,
            heartbeat_at=self.now,
            transport_operation="send_message",
            transport_failure_class="api_http",
            transport_status_code=429,
            transport_retry_after=17,
            transport_consecutive_failures=3,
            transport_success_at=self.now - timedelta(minutes=1),
        )
        self.assertEqual(
            (
                failed.transport_operation,
                failed.transport_failure_class,
                failed.transport_status_code,
                failed.transport_retry_after,
                failed.transport_consecutive_failures,
            ),
            ("send_message", "api_http", 429, 17, 3),
        )

        recovered = self.state.upsert_runtime_health(
            component="sender",
            instance_id="sender-transport",
            runtime="telegram",
            pid=1234,
            process_start_marker="sender-start",
            started_at=self.now,
            heartbeat_at=self.now + timedelta(seconds=1),
            transport_success_at=self.now + timedelta(seconds=1),
        )
        self.assertIsNone(recovered.transport_operation)
        self.assertIsNone(recovered.transport_failure_class)
        self.assertEqual(recovered.transport_consecutive_failures, 0)
        self.assertEqual(
            recovered.transport_success_at, (self.now + timedelta(seconds=1)).isoformat()
        )

    def test_projection_includes_expected_components_and_uses_cache_only(self) -> None:
        base = Path(self.tempdir.name)
        config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=base / "projects.json",
            state_path=base / "state.db",
            codex_socket_path=base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(),
            agents=(
                AgentDefinition(
                    "codex",
                    "Codex",
                    "example_codex_bot",
                    "codex",
                    None,
                    True,
                    False,
                    "gpt-example",
                    "high",
                ),
                AgentDefinition(
                    "opencode",
                    "OpenCode",
                    "example_open_bot",
                    "opencode",
                    None,
                    False,
                    False,
                    "provider-selected",
                    "high",
                ),
            ),
            dispatch_mode="queue",
            queue_runtime="external",
            outbox_runtime="external",
            external_worker_agent_ids=("codex", "opencode"),
        )
        self.state.upsert_runtime_health(
            component="controller",
            instance_id="project-hub-controller",
            pid=3456,
            process_start_marker="controller-start",
            started_at=self.now,
            heartbeat_at=self.now,
        )
        self.state.upsert_runtime_health(
            component="provider_worker",
            instance_id="codex-worker",
            runtime="codex",
            agent_id="codex",
            pid=4567,
            process_start_marker="codex-start",
            started_at=self.now - timedelta(minutes=5),
            heartbeat_at=self.now - timedelta(minutes=4),
        )

        projection = project_runtime_health(self.state, config, now=self.now)

        self.assertEqual(projection["controller"]["status"], "healthy")
        self.assertEqual(projection["sender"]["status"], "unknown")
        self.assertEqual(
            [(item["agent_id"], item["status"]) for item in projection["provider_workers"]],
            [("codex", "stale"), ("opencode", "unknown")],
        )
        self.assertEqual(projection["controller"]["pid"], 3456)
        self.assertNotIn("command_line", str(projection))
        self.assertNotIn("environment", str(projection))

        controller_outbox = replace(config, outbox_runtime="controller")
        self.assertEqual(
            project_runtime_health(self.state, controller_outbox, now=self.now)["sender"]["status"],
            "not_configured",
        )

        output = io.StringIO()
        with patch("hermes_codex_router.cli.load_hub_config", return_value=config):
            with redirect_stdout(output):
                self.assertEqual(main(["status", "example.json"]), 0)
        rendered = json.loads(output.getvalue())
        self.assertEqual(rendered["runtime_health"]["sender"]["status"], "unknown")
        self.assertEqual(len(rendered["runtime_health"]["provider_workers"]), 2)

    def test_status_never_invokes_optional_account_helper(self) -> None:
        base = Path(self.tempdir.name)
        config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=base / "projects.json",
            state_path=base / "state.db",
            codex_socket_path=base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(),
            agents=(),
            codex_multi_auth_dir=base / "optional-helper",
        )
        output = io.StringIO()
        with (
            patch("hermes_codex_router.cli.load_hub_config", return_value=config),
            patch(
                "hermes_codex_router.codex_accounts.read_codex_pool_status",
                side_effect=AssertionError("status must stay cache-only"),
            ),
            redirect_stdout(output),
        ):
            self.assertEqual(main(["status", "example.json"]), 0)
        self.assertNotIn("codex_account_pool", json.loads(output.getvalue()))

    def test_projection_degrades_mismatched_cached_worker_identity(self) -> None:
        base = Path(self.tempdir.name)
        config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=base / "projects.json",
            state_path=base / "state.db",
            codex_socket_path=base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(),
            agents=(
                AgentDefinition(
                    "codex",
                    "Codex",
                    "example_bot",
                    "codex",
                    None,
                    True,
                    False,
                    "gpt-example",
                    "high",
                ),
            ),
            dispatch_mode="queue",
            queue_runtime="external",
            external_worker_agent_ids=("codex",),
        )
        self.state.upsert_runtime_health(
            component="provider_worker",
            instance_id="codex-worker",
            runtime="opencode",
            agent_id="wrong-agent",
            pid=1234,
            process_start_marker="wrong-start",
            started_at=self.now,
            heartbeat_at=self.now,
        )
        worker = project_runtime_health(self.state, config, now=self.now)["provider_workers"][0]
        self.assertEqual(worker["status"], "degraded")
        self.assertTrue(worker["identity_mismatch"])
        self.assertEqual(worker["runtime"], "codex")
        self.assertEqual(worker["agent_id"], "codex")

    def test_deployment_revision_requires_one_complete_identity(self) -> None:
        base = Path(self.tempdir.name)
        config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=base / "projects.json",
            state_path=base / "state.db",
            codex_socket_path=base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(),
            agents=(
                AgentDefinition(
                    "codex",
                    "Codex",
                    "example_bot",
                    "codex",
                    None,
                    True,
                    False,
                    "gpt-example",
                    "high",
                ),
            ),
            dispatch_mode="queue",
            queue_runtime="external",
            outbox_runtime="external",
            external_worker_agent_ids=("codex",),
        )
        release = ReleaseIdentity("0.6.0", "a" * 40, "2026-09-04T12:00:00+00:00", True)
        components = (
            ("controller", "project-hub-controller", None, None),
            ("monitor", "operations-monitor", None, None),
            ("sender", "telegram-outbox-sender", "telegram", None),
            ("provider_worker", "codex-worker", "codex", "codex"),
        )
        for component, instance_id, runtime, agent_id in components:
            self.state.upsert_runtime_health(
                component=component,
                instance_id=instance_id,
                runtime=runtime,
                agent_id=agent_id,
                pid=1234,
                process_start_marker=f"{component}-start",
                started_at=self.now,
                heartbeat_at=self.now,
                release_identity=release,
            )

        projected = project_runtime_health(self.state, config, now=self.now)
        self.assertEqual(projected["deployment_revision"]["status"], "converged")
        self.assertEqual(projected["deployment_revision"]["git_sha"], "a" * 40)
        self.assertEqual(projected["deployment_revision"]["required_components"], 4)

        self.state.upsert_runtime_health(
            component="provider_worker",
            instance_id="codex-worker",
            runtime="codex",
            agent_id="codex",
            pid=1234,
            process_start_marker="codex-start",
            started_at=self.now,
            heartbeat_at=self.now,
            release_identity=ReleaseIdentity("0.6.0", "b" * 40, "2026-09-04T12:00:00+00:00", True),
        )
        self.assertEqual(
            project_runtime_health(self.state, config, now=self.now)["deployment_revision"][
                "status"
            ],
            "mixed",
        )

        self.state.upsert_runtime_health(
            component="monitor",
            instance_id="operations-monitor",
            pid=1234,
            process_start_marker="monitor-start",
            started_at=self.now,
            heartbeat_at=self.now,
            release_identity=ReleaseIdentity("0.6.0", None, None, False),
        )
        self.assertEqual(
            project_runtime_health(self.state, config, now=self.now)["deployment_revision"][
                "status"
            ],
            "unknown",
        )


if __name__ == "__main__":
    unittest.main()
