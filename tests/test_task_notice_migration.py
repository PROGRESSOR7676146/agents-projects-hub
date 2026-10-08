from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hermes_codex_router import migrations as migration
from hermes_codex_router import schema_task_lifecycle as lifecycle
from hermes_codex_router.root_blockers import RootBlocker, persistent_root_blocker


class TaskNoticeMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "state.db"
        connection = sqlite3.connect(self.path)
        with mock.patch.object(migration, "LATEST_SCHEMA_VERSION", 35):
            migration.migrate_connection(connection)
        try:
            self.old_ids: dict[str, str] = {}
            variants = (
                "pending",
                "sending",
                "attempted",
                "delivered",
                "partial",
                "missing_receipt",
                "failed",
                "long",
                "multipart_delivered",
                "provider",
            )
            for index, variant in enumerate(variants, start=1):
                job_id = f"job-{variant}"
                session_id = f"session-{variant}"
                old = "provider" if variant == "provider" else f"legacy-{variant}"
                with connection:
                    connection.execute(
                        """INSERT INTO topics
                        (topic_id,project_id,chat_id,thread_id,title,execution_scope,created_at,updated_at)
                        VALUES (?,'example-project',-1001234567890,?,'Fictional migration topic',
                                'root:/home/example/project','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')""",
                        (index, 70 + index),
                    )
                    connection.execute(
                        """INSERT INTO agent_sessions
                        (session_id,topic_id,agent_id,generation,status,model,effort,created_at,updated_at)
                        VALUES (?,?,'codex',1,'active','model','high','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')""",
                        (session_id, index),
                    )
                    connection.execute(
                        """INSERT INTO provider_jobs
                        (job_id,idempotency_key,chat_id,message_id,topic_id,topic_sequence,agent_id,
                         session_id,session_generation,model,effort,payload_text,status,error_class,
                         error_code,created_at,updated_at)
                        VALUES (?,?,-1001234567890,?,?,1,'codex',?,1,'model','high','Fictional work',
                                ?,?,?,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:01.5+00:00')""",
                        (
                            job_id,
                            f"fictional:{variant}",
                            index,
                            index,
                            session_id,
                            "result_ready" if variant == "provider" else "cancelled",
                            None if variant == "provider" else "user_stop",
                            None if variant == "provider" else "emergency_stop",
                        ),
                    )
                    if variant != "provider":
                        connection.execute(
                            """INSERT INTO provider_stop_requests
                            (request_id,topic_id,chat_id,message_id,target_agent_id,status,
                             cancelled_queued_count,created_at,completed_at)
                            VALUES (?,?,-1001234567890,?,'codex','completed',1,
                            '2026-01-01T00:00:01+00:00','2026-01-01T00:00:01.5+00:00')""",
                            (f"stop-{variant}", index, 100 + index),
                        )
                    else:
                        connection.execute(
                            """INSERT INTO provider_job_results(result_id,job_id,visible_response,created_at)
                            VALUES ('result-provider',?,'Fictional provider result','2026-01-01T00:00:02+00:00')""",
                            (job_id,),
                        )
                    connection.execute(
                        """INSERT INTO telegram_outbox
                        (outbox_id,job_id,sender_agent_id,chat_id,thread_id,telegram_html,status,
                         available_at,created_at,updated_at)
                        VALUES (?,? ,?,-1001234567890,?,?,'pending','2026-01-01T00:00:02+00:00',
                                '2026-01-01T00:00:02+00:00','2026-01-01T00:00:02+00:00')""",
                        (
                            old,
                            job_id,
                            "codex" if variant == "provider" else "hub",
                            70 + index,
                            "Provider result" if variant == "provider" else "Fictional stop",
                        ),
                    )
                    connection.execute(
                        "INSERT INTO telegram_outbox_parts(outbox_id,part_index,telegram_html) VALUES (?,1,?)",
                        (old, "Provider result" if variant == "provider" else "Fictional stop"),
                    )
                if variant == "provider":
                    continue
                self.old_ids[variant] = old
                with connection:
                    if variant == "sending":
                        connection.execute(
                            """UPDATE telegram_outbox SET status='sending',lease_owner='old-sender',
                            lease_token='old-token',lease_expires_at='2099-01-01' WHERE outbox_id=?""",
                            (old,),
                        )
                    elif variant == "attempted":
                        connection.execute(
                            "UPDATE telegram_outbox SET attempt_count=1,error_code='timeout' WHERE outbox_id=?",
                            (old,),
                        )
                    elif variant in {"delivered", "missing_receipt", "multipart_delivered"}:
                        connection.execute(
                            """UPDATE telegram_outbox SET status='delivered',attempt_count=1,
                            telegram_message_id=1001,delivered_at='later' WHERE outbox_id=?""",
                            (old,),
                        )
                        if variant != "missing_receipt":
                            connection.execute(
                                """UPDATE telegram_outbox_parts SET telegram_message_id=1001,
                                delivered_at='later' WHERE outbox_id=?""",
                                (old,),
                            )
                    elif variant == "failed":
                        connection.execute(
                            "UPDATE telegram_outbox SET status='failed',attempt_count=5,error_code='timeout' WHERE outbox_id=?",
                            (old,),
                        )
                    elif variant == "long":
                        connection.execute(
                            "UPDATE telegram_outbox SET telegram_html=? WHERE outbox_id=?",
                            ("x" * 3501, old),
                        )
                        connection.execute(
                            "UPDATE telegram_outbox_parts SET telegram_html=? WHERE outbox_id=?",
                            ("x" * 3501, old),
                        )
                    if variant in {"partial", "multipart_delivered"}:
                        connection.execute(
                            "UPDATE telegram_outbox_parts SET telegram_message_id=1001 WHERE outbox_id=?",
                            (old,),
                        )
                        connection.execute(
                            """INSERT INTO telegram_outbox_parts
                            (outbox_id,part_index,telegram_html,telegram_message_id)
                            VALUES (?,2,'Fictional tail',?)""",
                            (old, 1002 if variant == "multipart_delivered" else None),
                        )
        finally:
            connection.close()
        self.before = self._snapshot()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _snapshot(self) -> dict[str, list[tuple[object, ...]]]:
        with sqlite3.connect(self.path) as connection:
            return {
                table: connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
                for table in (
                    "provider_jobs",
                    "provider_job_results",
                    "provider_stop_requests",
                    "topics",
                    "telegram_outbox",
                    "telegram_outbox_parts",
                )
            }

    def _upgrade(
        self, *, fault: bool = False, create_backup: bool = True
    ) -> migration.MigrationResult:
        # Before lead-owned wiring lands, append the candidate through the same
        # transaction/script primitive. Once wired, exercise the real dispatcher.
        original_script = migration._execute_migration_script

        def execute(connection: sqlite3.Connection, script: str) -> None:
            original_script(connection, script)
            if fault and script == lifecycle.MIGRATION_36:
                raise RuntimeError("fictional migration-36 fault")

        def appended(connection: sqlite3.Connection) -> tuple[int, int]:
            previous = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if previous == 36:
                return previous, previous
            self.assertEqual(previous, 35)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                migration._execute_migration_script(connection, lifecycle.MIGRATION_36)
                connection.execute("PRAGMA user_version=36")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            return previous, 36

        with mock.patch.object(migration, "_execute_migration_script", execute):
            if migration.LATEST_SCHEMA_VERSION >= 36:
                return migration.migrate_database(self.path, create_backup=create_backup)
            with (
                mock.patch.object(migration, "LATEST_SCHEMA_VERSION", 36),
                mock.patch.object(migration, "migrate_connection", appended),
            ):
                return migration.migrate_database(self.path, create_backup=create_backup)

    def test_legacy_receipts_are_archived_and_never_restarted_blindly(self) -> None:
        result = self._upgrade()
        self.assertEqual(
            (result.previous_version, result.current_version), (35, migration.LATEST_SCHEMA_VERSION)
        )
        self.assertIsNotNone(result.backup_path)
        assert result.backup_path is not None
        self.assertEqual(result.backup_path.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(result.backup_path) as backup:
            self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 35)
            self.assertEqual(
                backup.execute("SELECT * FROM telegram_outbox ORDER BY 1,2").fetchall(),
                self.before["telegram_outbox"],
            )
        with sqlite3.connect(self.path) as connection:
            statuses = dict(
                connection.execute("SELECT event_key,status FROM task_lifecycle_notices")
            )
            expected = {
                "pending": "pending",
                "sending": "unknown",
                "attempted": "unknown",
                "delivered": "delivered",
                "partial": "unknown",
                "missing_receipt": "unknown",
                "failed": "failed",
                "long": "unknown",
                "multipart_delivered": "delivered",
            }
            for variant, status in expected.items():
                self.assertEqual(statuses[f"legacy-stop:{self.old_ids[variant]}"], status)
            links = dict(
                connection.execute(
                    "SELECT stop_request_id,notice_id FROM task_lifecycle_legacy_stop_links"
                )
            )
            for variant, outbox_id in self.old_ids.items():
                self.assertEqual(links[f"stop-{variant}"], f"legacy-stop:{outbox_id}")
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='task_lifecycle_migration_guard'"
                ).fetchone()
            )
            archived = connection.execute(
                "SELECT * FROM task_lifecycle_legacy_outbox ORDER BY 1,2"
            ).fetchall()
            self.assertEqual(
                archived, [row for row in self.before["telegram_outbox"] if row[2] == "hub"]
            )
            parts = connection.execute(
                "SELECT * FROM task_lifecycle_legacy_parts ORDER BY 1,2"
            ).fetchall()
            self.assertEqual(
                parts, [row for row in self.before["telegram_outbox_parts"] if row[0] != "provider"]
            )
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        after = self._snapshot()
        for table in ("provider_jobs", "provider_job_results", "provider_stop_requests", "topics"):
            expected = self.before[table]
            if table == "provider_jobs":
                expected = [(*row, None) for row in expected]
            self.assertEqual(after[table], expected)
        self.assertEqual(
            after["telegram_outbox"],
            [(*row, None) for row in self.before["telegram_outbox"] if row[0] == "provider"],
        )
        self.assertEqual(
            after["telegram_outbox_parts"],
            [(*row, 0) for row in self.before["telegram_outbox_parts"] if row[0] == "provider"],
        )
        self.assertEqual(self._upgrade().previous_version, migration.LATEST_SCHEMA_VERSION)

    def test_fault_after_archive_and_removal_rolls_back_in_place(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "migration-36 fault"):
            self._upgrade(fault=True)
        self.assertEqual(self._snapshot(), self.before)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 35)
            self.assertIsNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='task_lifecycle_notices'"
                ).fetchone()
            )
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_unrecognized_hub_row_aborts_without_relabeling_or_deletion(self) -> None:
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "UPDATE telegram_outbox SET chat_id=-1001111111111 WHERE outbox_id=?",
                (self.old_ids["pending"],),
            )
        before = self._snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self._upgrade()
        self.assertEqual(self._snapshot(), before)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 35)

    def test_duplicate_stop_links_exclude_later_idle_requests(self) -> None:
        with sqlite3.connect(self.path) as connection:
            for variant, active in (("pending", False), ("sending", True)):
                old = self.old_ids[variant]
                job_id = connection.execute(
                    "SELECT job_id FROM telegram_outbox WHERE outbox_id=?", (old,)
                ).fetchone()[0]
                connection.execute(
                    "UPDATE provider_jobs SET created_at='2026-01-01T00:00:00+00:00',updated_at='2026-01-01T00:03:00+00:00' WHERE job_id=?",
                    (job_id,),
                )
                connection.execute(
                    "UPDATE telegram_outbox SET created_at='2026-01-01T00:01:00+00:00' WHERE outbox_id=?",
                    (old,),
                )
                topic = connection.execute(
                    "SELECT topic_id FROM provider_jobs WHERE job_id=?", (job_id,)
                ).fetchone()[0]
                connection.execute(
                    "UPDATE provider_stop_requests SET created_at='2026-01-01T00:00:30+00:00' WHERE topic_id=?",
                    (topic,),
                )
                if active:
                    connection.execute(
                        """UPDATE provider_jobs SET status='executing',lease_owner='worker',
                    lease_token='token',lease_expires_at='2099-01-01' WHERE job_id=?""",
                        (job_id,),
                    )
                for suffix, when, status in (
                    (
                        "duplicate",
                        "2026-01-01T00:02:00+00:00",
                        "pending" if active else "completed",
                    ),
                    ("idle", "2026-01-01T00:04:00+00:00", "completed"),
                ):
                    connection.execute(
                        """INSERT INTO provider_stop_requests
                    (request_id,topic_id,chat_id,message_id,target_agent_id,status,created_at)
                    VALUES (?,?, -1001234567890,?,'codex',?,?)""",
                        (
                            f"{variant}-{suffix}",
                            topic,
                            900 + (10 if active else 0) + (1 if suffix == "idle" else 0),
                            status,
                            when,
                        ),
                    )
        self._upgrade()
        with sqlite3.connect(self.path) as connection:
            links = dict(
                connection.execute(
                    "SELECT stop_request_id,notice_id FROM task_lifecycle_legacy_stop_links"
                )
            )
        for variant in ("pending", "sending"):
            self.assertEqual(links[f"{variant}-duplicate"], f"legacy-stop:{self.old_ids[variant]}")
            self.assertNotIn(f"{variant}-idle", links)

    def test_terminal_evidence_is_preserved_and_completed_proof_is_additive(self) -> None:
        with sqlite3.connect(self.path) as connection:
            proof_job = connection.execute(
                "SELECT job_id FROM telegram_outbox WHERE outbox_id='provider'"
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO provider_turn_terminal_evidence
            VALUES (?, 'failed','fictional-thread','fictional-turn','/home/example/project','earlier')""",
                (proof_job,),
            )
            unknown_job = connection.execute(
                "SELECT job_id FROM telegram_outbox WHERE outbox_id=?", (self.old_ids["pending"],)
            ).fetchone()[0]
            connection.execute(
                "UPDATE provider_jobs SET status='indeterminate',error_class='ambiguous_execution',error_code='unconfirmed' WHERE job_id=?",
                (unknown_job,),
            )
            topic = connection.execute(
                "SELECT topic_id FROM provider_jobs WHERE job_id=?", (unknown_job,)
            ).fetchone()[0]
            connection.row_factory = sqlite3.Row
            # Current facades require current schema; historical native-uncertainty
            # expectation comes from the literal fixture, without runtime bridging.
            destination = connection.execute(
                "SELECT chat_id,thread_id FROM topics WHERE topic_id=?", (topic,)
            ).fetchone()
            blocker_before = RootBlocker(
                "uncertain", topic, destination[0], destination[1], None, None, unknown_job
            )
            evidence_before = [
                tuple(row)
                for row in connection.execute("SELECT * FROM provider_turn_terminal_evidence")
            ]
        self._upgrade()
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(
                connection.execute("SELECT * FROM provider_turn_terminal_evidence").fetchall(),
                evidence_before,
            )
            connection.row_factory = sqlite3.Row
            self.assertEqual(persistent_root_blocker(connection, topic_id=topic), blocker_before)
            connection.execute(
                """INSERT INTO provider_turn_terminal_evidence
            VALUES (?, 'completed','fictional-thread-2','fictional-turn-2','/home/example/project','later')""",
                (unknown_job,),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE provider_turn_terminal_evidence SET terminal_status='active' WHERE job_id=?",
                    (unknown_job,),
                )

    def test_time_mismatch_and_held_work_cannot_be_inferred_as_legacy_stop(self) -> None:
        for mismatch in ("time", "held"):
            with self.subTest(mismatch=mismatch):
                with sqlite3.connect(self.path) as connection:
                    old = self.old_ids["pending"]
                    job_id = connection.execute(
                        "SELECT job_id FROM telegram_outbox WHERE outbox_id=?", (old,)
                    ).fetchone()[0]
                    if mismatch == "time":
                        connection.execute(
                            "UPDATE telegram_outbox SET created_at='1900-01-01' WHERE outbox_id=?",
                            (old,),
                        )
                    else:
                        connection.execute(
                            "UPDATE telegram_outbox SET created_at='2099-01-01' WHERE outbox_id=?",
                            (old,),
                        )
                        connection.execute(
                            "INSERT INTO provider_job_holds(job_id,cause_job_id,held_at,hold_reason,decision) VALUES (?,?,'1900-01-01','uncertainty','pending')",
                            (job_id, job_id),
                        )
                before = self._snapshot()
                with self.assertRaises(sqlite3.IntegrityError):
                    self._upgrade(create_backup=False)
                self.assertEqual(self._snapshot(), before)
