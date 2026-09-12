from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from schema_fixtures import remove_adoption_schema
from test_deployment_manifest import _wheel

from hermes_codex_router import migrations
from hermes_codex_router.deployment_manifest import inspect_wheel
from hermes_codex_router.release_dry_run import (
    ReleaseDryRunError,
    _extract_wheel,
    _run_artifact_migration,
)
from hermes_codex_router.session_adoption_state import AdoptionRequest, CodexSessionOrigins
from hermes_codex_router.state import HubState


class SessionAdoptionMigrationTests(unittest.TestCase):
    def test_v24_upgrade_preserves_history_and_failed_ddl_rolls_back(self) -> None:
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.db"
                state = HubState.open(path)
                try:
                    topic = state.observe_topic(
                        project_id="example", chat_id=-1001, thread_id=7, title="Example"
                    )
                    state.activate_agent(topic.topic_id, "codex", "model", "high")
                    state.record_forwarded_quote(
                        topic_id=topic.topic_id,
                        chat_id=-1001,
                        message_id=1,
                        observer_agent_id="hub",
                        text="Existing visible quote",
                    )
                finally:
                    state.close()
                with closing(sqlite3.connect(path)) as connection, connection:
                    remove_adoption_schema(connection)
                    connection.execute("PRAGMA user_version=24")
                    before = connection.execute("SELECT * FROM agent_sessions").fetchall()
                if fail:
                    with (
                        patch.object(
                            migrations, "MIGRATION_25", migrations.MIGRATION_25 + "\nINVALID DDL;\n"
                        ),
                        self.assertRaises(sqlite3.DatabaseError),
                    ):
                        migrations.migrate_database(path)
                else:
                    result = migrations.migrate_database(path)
                    self.assertEqual((result.previous_version, result.current_version), (24, 25))
                    assert result.backup_path is not None
                    with closing(sqlite3.connect(result.backup_path)) as backup:
                        self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 24)
                with closing(sqlite3.connect(path)) as connection:
                    self.assertEqual(
                        connection.execute("PRAGMA user_version").fetchone()[0], 24 if fail else 25
                    )
                    self.assertEqual(
                        connection.execute("SELECT * FROM agent_sessions").fetchall(), before
                    )
                    self.assertEqual(
                        connection.execute(
                            "SELECT response_excerpt FROM external_turn_excerpts"
                        ).fetchone()[0],
                        "Existing visible quote",
                    )
                    self.assertEqual(
                        connection.execute("PRAGMA integrity_check").fetchone()[0], "ok"
                    )
                    columns = {
                        row[1]
                        for row in connection.execute("PRAGMA table_info(external_turn_excerpts)")
                    }
                    self.assertEqual("source_message_id" in columns, not fail)
                    if not fail:
                        self.assertIsNone(
                            connection.execute(
                                "SELECT source_message_id FROM external_turn_excerpts"
                            ).fetchone()[0]
                        )

    def test_distinct_rollback_fixture_retains_policy_and_v24_refuses_origins(self) -> None:
        # A policy rehearsal, not a released/accepted production rollback wheel.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_path = root / "state.db"
            state = HubState.open(state_path)
            try:
                topic = state.observe_topic(
                    project_id="example", chat_id=-1001, thread_id=7, title="Example"
                )
                attached = CodexSessionOrigins(state).attach(
                    AdoptionRequest("example", -1001, 7, "example-thread", root, "model", "high"),
                    expected_session_id=None,
                )
                state.return_codex_local_writer(
                    chat_id=-1001,
                    message_id=10,
                    topic_id=topic.topic_id,
                    session_id=attached.session.session_id,
                    observer_agent_id="hub",
                )
                state.new_active_session(topic.topic_id)
                origin_before = state._connection.execute(
                    "SELECT * FROM codex_session_origins"
                ).fetchall()
            finally:
                state.close()
            package = Path(__file__).resolve().parents[1] / "src/hermes_codex_router"
            descriptors = []
            for index, maximum in enumerate((25, 25, 24)):
                wheel = _wheel(
                    root / f"fixture-{index}.whl",
                    version=f"0.7.{index}",
                    git_sha="abc"[index] * 40,
                    schema_max=maximum,
                )
                with zipfile.ZipFile(wheel, "a") as archive:
                    for name in (
                        "__init__.py",
                        "migrations.py",
                        "models.py",
                        "registry.py",
                        "session_adoption_policy.py",
                    ):
                        archive.writestr(
                            f"hermes_codex_router/{name}", (package / name).read_bytes()
                        )
                descriptors.append(inspect_wheel(wheel))
                extracted = root / f"artifact-{index}"
                _extract_wheel(wheel, extracted)
                if maximum == 24:
                    with self.assertRaises(ReleaseDryRunError):
                        _run_artifact_migration(extracted, state_path)
                    continue
                self.assertEqual(_run_artifact_migration(extracted, state_path)["state_schema"], 25)
                script = """
import sys
from pathlib import Path
from types import SimpleNamespace as Config
from hermes_codex_router.session_adoption_policy import validate_adoption_mode
config = Config(state_path=Path(sys.argv[1]), dispatch_mode='inline')
config.require_agent = lambda _: Config(runtime='codex', managed_externally=False)
try:
    validate_adoption_mode(config)
except ValueError:
    print('policy-refused')
else:
    raise AssertionError('archived origin lost its execution policy')
"""
                result = subprocess.run(
                    (sys.executable, "-c", script, str(state_path)),
                    cwd=extracted,
                    env={
                        "PATH": os.environ.get("PATH", ""),
                        "PYTHONPATH": str(extracted),
                        "PYTHONNOUSERSITE": "1",
                    },
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=True,
                )
                self.assertEqual(result.stdout.strip(), "policy-refused")
            self.assertNotEqual(descriptors[0].sha256, descriptors[1].sha256)
            state = HubState.open(state_path)
            try:
                self.assertEqual(
                    state._connection.execute("SELECT * FROM codex_session_origins").fetchall(),
                    origin_before,
                )
            finally:
                state.close()
