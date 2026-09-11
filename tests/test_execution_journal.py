from __future__ import annotations

import multiprocessing
import os
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import test_codex_worker as fixtures
import test_embedded_queue_service as embedded_fixtures
from test_codex_appserver import FakeTransport

from hermes_codex_router.codex_appserver import CodexAppServerClient, TurnResult
from hermes_codex_router.codex_worker import CodexQueueWorker
from hermes_codex_router.state import HubState, StateError


def crash_worker(config: Any, registry: Any, phase: str) -> None:
    class Client(fixtures.WorkerClient):
        def start_turn(self, **kwargs: Any) -> str:
            if phase == "unacknowledged":
                os._exit(17)
            return super().start_turn(**kwargs)

        def wait_for_turn(self, turn_id: str) -> TurnResult:
            if phase != "accepted":
                cast(Any, self).on_visible_item("visible-1", "Saved progress", "commentary")
            if phase == "completed":
                cast(Any, self).on_completed(TurnResult("Saved final", None, None))
            os._exit(17)

    worker = CodexQueueWorker(
        config,
        registry=registry,
        supervisor=cast(Any, fixtures.WorkerSupervisor(Client())),
        worker_id="crash-worker",
    )
    worker.run_cycle()


class ExecutionJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.CodexQueueWorkerTests()
        self.fixture.setUp()

    def tearDown(self) -> None:
        self.fixture.tearDown()

    def test_execution_journal_migration_is_additive_and_preserves_queued_work(self) -> None:
        from hermes_codex_router.migrations import LATEST_SCHEMA_VERSION, migrate_database

        job_id = self.fixture.enqueue()
        path = self.fixture.config.state_path
        with sqlite3.connect(path) as con:
            con.execute("DROP TABLE IF EXISTS provider_visible_items")
            con.execute("DROP TABLE IF EXISTS provider_execution_checkpoints")
            con.execute("PRAGMA user_version = 21")
        migrated = migrate_database(path)
        self.assertEqual(
            (migrated.previous_version, migrated.current_version),
            (21, LATEST_SCHEMA_VERSION),
        )
        assert migrated.backup_path is not None
        with sqlite3.connect(migrated.backup_path) as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 21)
        state = HubState.open(path)
        try:
            self.assertEqual(state.get_provider_job(job_id).status, "queued")
        finally:
            state.close()

    def test_abrupt_exit_recovers_completed_or_partial_without_reinvocation(self) -> None:
        for phase in ("unacknowledged", "accepted", "partial", "completed"):
            with self.subTest(phase=phase):
                fixture = fixtures.CodexQueueWorkerTests()
                fixture.setUp()
                try:
                    job_id = fixture.enqueue()
                    process = multiprocessing.get_context("fork").Process(
                        target=crash_worker, args=(fixture.config, fixture.registry, phase)
                    )
                    process.start()
                    process.join(8)
                    if process.is_alive():
                        process.kill()
                        process.join(3)
                    self.assertEqual(process.exitcode, 17)
                    state = HubState.open(fixture.config.state_path)
                    job = state.get_provider_job(job_id)
                    self.assertIsNotNone(state.get_session(job.session_id).provider_session_id)
                    assert job.lease_token is not None
                    state.heartbeat_provider_job(
                        job_id,
                        job.lease_token,
                        lease_seconds=1,
                        now=datetime.now(timezone.utc) - timedelta(seconds=10),
                    )
                    state.close()

                    class Reader(fixtures.WorkerClient):
                        reads = 0

                        def read_completed_turn(self, **kwargs: Any) -> None:
                            self.reads += 1
                            return None

                    client = Reader()
                    worker = fixture.worker(client)
                    try:
                        worker.run_cycle()
                        result = worker.state.get_provider_job(job_id)
                        self.assertEqual(
                            result.status,
                            "result_ready" if phase == "completed" else "indeterminate",
                        )
                        outbox = worker.state.get_telegram_outbox_for_job(job_id)
                        if phase in {"partial", "completed"}:
                            self.assertIn(
                                "Saved final" if phase == "completed" else "Saved progress",
                                outbox.telegram_html,
                            )
                        self.assertEqual(client.turns, 0)
                        self.assertEqual(client.reads, 1 if phase in {"accepted", "partial"} else 0)
                        self.assertFalse(worker.run_cycle())
                    finally:
                        worker.close()
                finally:
                    fixture.tearDown()

    def test_thread_read_checks_exact_identity_and_excludes_tool_output(self) -> None:
        thread = {
            "id": "thread-1",
            "cwd": str(self.fixture.registry.projects[0].root),
            "turns": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "items": [
                        {"id": "tool", "type": "commandExecution", "text": "private tool output"},
                        {"id": "visible", "type": "agentMessage", "text": "Recovered final"},
                    ],
                }
            ],
        }
        for sid, turn, expected in (
            ("thread-1", "turn-1", True),
            ("thread-1", "other-turn", False),
        ):
            transport = FakeTransport([{"id": 1, "result": {"thread": thread}}])
            client = CodexAppServerClient(transport, initialized=True)
            result = client.read_completed_turn(
                thread_id=sid, turn_id=turn, cwd=self.fixture.registry.projects[0].root
            )
            self.assertEqual(
                result.text if result else None, "Recovered final" if expected else None
            )
            self.assertEqual([x["method"] for x in transport.sent], ["thread/read"])

    def test_embedded_restart_recovers_checkpoint_before_dispatching_new_work(self) -> None:
        class Client(embedded_fixtures.QueueClient):
            def wait_for_turn(self, turn_id: str) -> TurnResult:
                cast(Any, self).on_completed(TurnResult("Embedded recovered final", None, None))
                os._exit(17)

        fixture = embedded_fixtures.EmbeddedQueueServiceTests()
        fixture.setUp()
        client = Client()
        service, telegram = fixture.service(client)
        try:
            service.handle_update(embedded_fixtures.update(1, "Fictional task"))
            process = multiprocessing.get_context("fork").Process(
                target=service.run_embedded_queue_cycle
            )
            process.start()
            process.join(8)
            if process.is_alive():
                process.kill()
                process.join(3)
            self.assertEqual(process.exitcode, 17)
            topic = service.state.find_topic(-1001234567890, 77)
            assert topic is not None
            job = service.state.provider_jobs_for_topic(topic.topic_id)[0]
            assert job.lease_token
            service.state.heartbeat_provider_job(
                job.job_id,
                job.lease_token,
                lease_seconds=1,
                now=datetime.now(timezone.utc) - timedelta(seconds=10),
            )
            self.assertTrue(service.run_embedded_queue_cycle())
            self.assertEqual(service.state.get_provider_job(job.job_id).status, "result_ready")
            self.assertTrue(service.run_embedded_queue_cycle())
            self.assertEqual(service.state.get_provider_job(job.job_id).status, "completed")
            self.assertTrue(any("Embedded recovered final" in text for text in telegram.sent))
            self.assertEqual(client.turn_threads, [])
        finally:
            service.close()
            fixture.tearDown()

    def test_provider_read_recovers_after_acceptance_without_reinvocation(self) -> None:
        from hermes_codex_router.execution_journal import ExecutionJournal

        job_id = self.fixture.enqueue()
        state = HubState.open(self.fixture.config.state_path)
        lease = state.lease_provider_job("codex", "old-worker")
        assert lease and lease.lease_token
        state.mark_provider_job_executing(job_id, lease.lease_token)
        journal = ExecutionJournal(state)
        journal.record_thread(
            job_id, lease.lease_token, "thread-1", self.fixture.registry.projects[0].root
        )
        journal.record_turn(job_id, lease.lease_token, "turn-1")
        state.heartbeat_provider_job(
            job_id,
            lease.lease_token,
            lease_seconds=1,
            now=datetime.now(timezone.utc) - timedelta(seconds=10),
        )
        state.close()

        class Reader(fixtures.WorkerClient):
            reads = 0

            def read_completed_turn(self, **kwargs: Any) -> TurnResult:
                self.reads += 1
                return TurnResult("Recovered by exact ID", None, None)

        client = Reader()
        worker = self.fixture.worker(client)
        try:
            self.assertTrue(worker.run_cycle())
            self.assertEqual(client.reads, 1)
            self.assertEqual(client.turns, 0)
            self.assertEqual(worker.state.get_provider_job(job_id).status, "result_ready")
        finally:
            worker.close()

    def test_wrong_thread_root_and_unfinished_turn_never_become_success(self) -> None:
        from hermes_codex_router.codex_appserver import RpcError

        root = self.fixture.registry.projects[0].root
        for thread_id, cwd, status in (
            ("other-thread", str(root), "completed"),
            ("thread-1", str(root.parent), "completed"),
            ("thread-1", str(root), "inProgress"),
        ):
            transport = FakeTransport(
                [
                    {
                        "id": 1,
                        "result": {
                            "thread": {
                                "id": thread_id,
                                "cwd": cwd,
                                "turns": [{"id": "turn-1", "status": status, "items": []}],
                            }
                        },
                    }
                ]
            )
            client = CodexAppServerClient(transport, initialized=True)
            if status == "inProgress":
                self.assertIsNone(
                    client.read_completed_turn(thread_id="thread-1", turn_id="turn-1", cwd=root)
                )
            else:
                with self.assertRaises(RpcError):
                    client.read_completed_turn(thread_id="thread-1", turn_id="turn-1", cwd=root)

    def test_migration_ddl_failure_restores_schema_21_in_place(self) -> None:
        from hermes_codex_router.migrations import migrate_database

        path = self.fixture.config.state_path
        self.fixture.enqueue()
        with sqlite3.connect(path) as con:
            con.execute("DROP TABLE provider_visible_items")
            con.execute("DROP TABLE provider_execution_checkpoints")
            con.execute("PRAGMA user_version = 21")
        with patch(
            "hermes_codex_router.migrations.MIGRATION_22",
            "CREATE TABLE checkpoint_probe (id INTEGER); INVALID DDL;",
        ):
            with self.assertRaises(sqlite3.DatabaseError):
                migrate_database(path)
        with sqlite3.connect(path) as con:
            self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 21)
            self.assertEqual(
                con.execute(
                    "SELECT name FROM sqlite_master WHERE name='checkpoint_probe'"
                ).fetchall(),
                [],
            )

    def test_rollout_rejects_old_rollback_and_rehearses_current_schema_artifacts(self) -> None:
        from hermes_codex_router.deployment_manifest import DeploymentManifestError
        from hermes_codex_router.release_dry_run import run_release_dry_run
        from tests.test_deployment_manifest import _wheel

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            artifacts = []
            package = Path(__file__).resolve().parents[1] / "src/hermes_codex_router"
            for version, sha in (("0.7.0", "a" * 40), ("0.6.0", "b" * 40)):
                wheel = _wheel(
                    base / f"agents_projects_hub-{version}-py3-none-any.whl",
                    version=version,
                    git_sha=sha,
                    schema_max=23,
                )
                with zipfile.ZipFile(wheel, "a") as archive:
                    for name in ("__init__.py", "migrations.py", "models.py", "registry.py"):
                        archive.writestr(
                            f"hermes_codex_router/{name}", (package / name).read_text()
                        )
                artifacts.append(wheel)
            report = run_release_dry_run(*artifacts)
            self.assertEqual(report.schema_after_rollout, 23)
            self.assertEqual(report.schema_after_rollback, 23)
            self.assertTrue(report.durable_work_preserved)
            old = _wheel(
                base / "agents_projects_hub-0.5.0-py3-none-any.whl",
                version="0.5.0",
                git_sha="c" * 40,
                schema_max=21,
            )
            with self.assertRaises(DeploymentManifestError):
                run_release_dry_run(artifacts[0], old)

    def test_checkpoint_lease_identity_and_visible_only_guards(self) -> None:
        from hermes_codex_router.execution_journal import ExecutionJournal

        job_id = self.fixture.enqueue()
        state = HubState.open(self.fixture.config.state_path)
        try:
            lease = state.lease_provider_job("codex", "worker")
            assert lease and lease.lease_token
            state.mark_provider_job_executing(job_id, lease.lease_token)
            journal = ExecutionJournal(state)
            cwd = self.fixture.registry.projects[0].root
            journal.record_thread(job_id, lease.lease_token, "thread-1", cwd)
            journal.record_turn(job_id, lease.lease_token, "turn-1")
            self.assertIsNone(state.get_provider_job(job_id).provider_session_id)
            journal.record_item(job_id, lease.lease_token, "item-1", "visible", "commentary")
            journal.record_item(job_id, lease.lease_token, "item-1", "visible", "commentary")
            self.assertEqual(journal.partial_text(job_id), "visible")
            with self.assertRaises(StateError):
                journal.record_item(job_id, "wrong-token", "item-2", "x", "commentary")
            with self.assertRaises(StateError):
                journal.record_item(job_id, lease.lease_token, "item-2", "x", "reasoning")
            with self.assertRaises(StateError):
                journal.record_turn(job_id, lease.lease_token, "other-turn")
            with self.assertRaises(StateError):
                journal.record_item(
                    job_id, lease.lease_token, "large-item", "x" * 200_000, "commentary"
                )
            state.heartbeat_provider_job(
                job_id,
                lease.lease_token,
                lease_seconds=1,
                now=datetime.now(timezone.utc) - timedelta(seconds=10),
            )
            recovered = journal.claim_stale("codex", "recovery-worker")
            assert recovered is not None
            self.assertEqual(recovered.attempt_count, 1)
            with self.assertRaises(StateError):
                journal.record_completion(job_id, lease.lease_token, "Late stale result")
            self.assertEqual(journal.partial_text(job_id), "visible")
        finally:
            state.close()
