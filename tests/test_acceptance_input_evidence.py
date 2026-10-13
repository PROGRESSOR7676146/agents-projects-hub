from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from hermes_codex_router.acceptance_actor import _run_check, run_acceptance_checks
from hermes_codex_router.acceptance_contracts import (
    AcceptanceActorConfig,
    AcceptanceActorError,
    AcceptanceCheckResult,
)
from hermes_codex_router.acceptance_runtime import AcceptanceRuntimeError, ReadOnlyAcceptanceState
from hermes_codex_router.p0_p1_acceptance import P0P1ScenarioContext, run_p0_p1_live_scenario
from hermes_codex_router.schema_compatibility import TARGET_SCHEMA_VERSION
from tests.test_acceptance_actor import (
    FakeClient,
    FakeMessage,
    FakeP0P1Client,
    StatefulAcceptanceProbe,
    StatefulServiceSupervisor,
)


class ControlStateProbe(StatefulAcceptanceProbe):
    def __init__(self, path: Path, *, fail_message_id: int | None = None) -> None:
        super().__init__(fail_message_id=fail_message_id)
        self.control_state = ReadOnlyAcceptanceState(path)

    def jobs_for_input(self, chat_id: int, message_id: int) -> list[tuple[str, str]]:
        if message_id not in {109, 110}:
            return super().jobs_for_input(chat_id, message_id)
        self.calls.append(("jobs", message_id))
        if message_id == self.fail_message_id:
            raise AcceptanceRuntimeError("named control state failure")
        return self.control_state.jobs_for_input(chat_id, message_id)


class AuthorizedClient(FakeClient):
    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def is_user_authorized(self) -> bool:
        return True

    async def get_me(self) -> SimpleNamespace:
        return SimpleNamespace(id=1)


class AcceptanceInputEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.base = Path(self.tempdir.name)
        self.state_path = self.base / "state.db"
        with sqlite3.connect(self.state_path) as connection:
            connection.executescript(
                f"""
                PRAGMA user_version={TARGET_SCHEMA_VERSION};
                CREATE TABLE provider_jobs (
                    job_id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE provider_job_inputs (
                    job_id TEXT NOT NULL, chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL
                );
                INSERT INTO provider_jobs VALUES ('other-chat', 'executing', '2026-01-01');
                INSERT INTO provider_jobs VALUES ('other-input', 'queued', '2026-01-01');
                INSERT INTO provider_job_inputs VALUES ('other-chat', -1000000000001, 109);
                INSERT INTO provider_job_inputs VALUES ('other-input', -1001234567890, 500);
                """
            )
        self.state_path.chmod(0o600)
        self.config = AcceptanceActorConfig(
            api_id=1,
            api_hash_file=self.base / "telegram-api-hash",
            session_path=self.base / "acceptance.session",
            expected_user_id=1,
            telegram_chat_id=-1001234567890,
            telegram_thread_id=77,
            hub_username="example_hub_bot",
            provider_usernames=("example_codex_bot",),
            checks=("status", "accounts"),
            timeout_seconds=180,
            artifacts_dir=self.base,
            provider_agent_ids=("codex",),
            state_path=self.state_path,
            allow_service_restart=True,
        )

    def run_scenario(
        self,
        *,
        fail_message_id: int | None = None,
        fail_restore: bool = False,
        accounts_timeout: bool = False,
    ) -> tuple[list[AcceptanceCheckResult], FakeP0P1Client, StatefulServiceSupervisor]:
        client = FakeP0P1Client()
        supervisor = StatefulServiceSupervisor(fail_restore=fail_restore)
        probe = ControlStateProbe(self.state_path, fail_message_id=fail_message_id)
        responses = tuple(FakeMessage(201 + index, "fixture") for index in range(6)) + (
            FakeMessage(207, "Context remaining: 88.5%\nWeekly remaining: 42%"),
            FakeMessage(208, "Context remaining: 88.5%"),
        )
        accounts_wait = (
            AsyncMock(side_effect=AcceptanceActorError("accounts response timed out"))
            if accounts_timeout
            else AsyncMock(return_value=FakeMessage(209, "No provider accounts are configured."))
        )
        with (
            patch("hermes_codex_router.p0_p1_acceptance._select_provider", new=AsyncMock()),
            patch(
                "hermes_codex_router.p0_p1_acceptance._wait_for_markers",
                new=AsyncMock(side_effect=responses),
            ),
            patch("hermes_codex_router.p0_p1_acceptance._wait_for_matching", new=accounts_wait),
        ):
            results = asyncio.run(
                run_p0_p1_live_scenario(
                    P0P1ScenarioContext(client, self.config, probe, supervisor),
                    "example_codex_bot",
                )
            )
        return results, client, supervisor

    def add_control_job(self, message_id: int, status: str) -> None:
        with sqlite3.connect(self.state_path) as connection:
            connection.execute("DELETE FROM provider_job_inputs WHERE job_id='control'")
            connection.execute("DELETE FROM provider_jobs WHERE job_id='control'")
            connection.execute(
                "INSERT INTO provider_jobs VALUES ('control', ?, '2026-01-02')", (status,)
            )
            connection.execute(
                "INSERT INTO provider_job_inputs VALUES ('control', ?, ?)",
                (self.config.telegram_chat_id, message_id),
            )

    def test_private_report_records_control_inputs_without_claiming_state_observation(self) -> None:
        client = AuthorizedClient()
        with (
            patch("telethon.TelegramClient", return_value=client),
            patch("hermes_codex_router.acceptance_actor._api_hash", return_value="0" * 32),
            patch(
                "hermes_codex_router.acceptance_actor._wait_for_response",
                new=AsyncMock(return_value=FakeMessage(10, "deterministic reply")),
            ),
        ):
            summary = asyncio.run(run_acceptance_checks(self.config))
        artifact = Path(str(summary["artifact"]))
        results = json.loads(artifact.read_text())["results"]
        self.assertEqual([result["input_message_ids"] for result in results], [[1], [2]])
        self.assertEqual([result["provider_job_observed"] for result in results], [None, None])
        self.assertEqual(artifact.stat().st_mode & 0o777, 0o600)

    def test_control_timeouts_retain_the_sent_input(self) -> None:
        for check in ("status", "accounts"):
            with (
                self.subTest(check),
                patch(
                    "hermes_codex_router.acceptance_actor._wait_for_response",
                    new=AsyncMock(side_effect=AcceptanceActorError("response timed out")),
                ),
            ):
                result = asyncio.run(
                    _run_check(FakeClient(), self.config, check, "example_hub_bot")
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.input_message_ids, (1,))
                self.assertIsNone(result.provider_job_observed)

    def test_reply_route_retains_both_inputs_when_the_child_times_out(self) -> None:
        with patch(
            "hermes_codex_router.acceptance_actor._wait_for_response",
            new=AsyncMock(
                side_effect=(
                    FakeMessage(10, "REPLY_PARENT_OK"),
                    AcceptanceActorError("child timeout"),
                )
            ),
        ):
            result = asyncio.run(
                _run_check(FakeClient(), self.config, "reply_route", "example_codex_bot")
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.input_message_ids, (1, 2))

    def test_controls_ignore_other_jobs_and_leave_the_real_sqlite_fixture_unchanged(self) -> None:
        before = self.state_path.read_bytes()
        results, _, _ = self.run_scenario()
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(results[-1].input_message_ids, (109, 110))
        self.assertIs(results[-1].provider_job_observed, False)
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_any_status_job_fails_before_accounts_and_survives_restoration_failure(self) -> None:
        for status in ("queued", "executing", "completed", "cancelled", "indeterminate"):
            with self.subTest(status):
                self.add_control_job(109, status)
                results, client, supervisor = self.run_scenario(fail_restore=True)
                result = results[-1]
                self.assertFalse(result.ok)
                self.assertEqual(result.input_message_ids, (109,))
                self.assertIs(result.provider_job_observed, True)
                self.assertIn("/status", result.detail)
                self.assertIn("service restoration failed", result.detail)
                self.assertFalse(any("/accounts@" in str(args[1]) for args, _ in client.sent))
                self.assertEqual(supervisor.actions[-1], "restore")
                with sqlite3.connect(self.state_path) as connection:
                    connection.execute("DELETE FROM provider_job_inputs WHERE job_id='control'")
                    connection.execute("DELETE FROM provider_jobs WHERE job_id='control'")

    def test_accounts_job_fails_with_both_inputs(self) -> None:
        self.add_control_job(110, "completed")
        results, _, supervisor = self.run_scenario()
        self.assertFalse(results[-1].ok)
        self.assertEqual(results[-1].input_message_ids, (109, 110))
        self.assertIs(results[-1].provider_job_observed, True)
        self.assertIn("/accounts", results[-1].detail)
        self.assertEqual(supervisor.actions[-1], "restore")

    def test_control_probe_failure_remains_unknown_and_preserves_sent_inputs(self) -> None:
        for message_id, expected_inputs in ((109, (109,)), (110, (109, 110))):
            with self.subTest(message_id):
                results, _, _ = self.run_scenario(fail_message_id=message_id)
                self.assertFalse(results[-1].ok)
                self.assertEqual(results[-1].input_message_ids, expected_inputs)
                self.assertIsNone(results[-1].provider_job_observed)
                self.assertIn("named control state failure", results[-1].detail)

    def test_partial_negative_observation_is_not_reported_as_verified_absence(self) -> None:
        results, _, _ = self.run_scenario(accounts_timeout=True)
        self.assertFalse(results[-1].ok)
        self.assertEqual(results[-1].input_message_ids, (109, 110))
        self.assertIsNone(results[-1].provider_job_observed)


if __name__ == "__main__":
    unittest.main()
