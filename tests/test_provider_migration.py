from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import migrations


class ProviderMigrationTests(unittest.TestCase):
    def test_v29_upgrade_preserves_origins_and_triggers_and_fault_rolls_back(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "state.db"
                with patch.object(migrations, "LATEST_SCHEMA_VERSION", 29):
                    # Construct exactly the historical schema, not a relabelled new one.
                    connection = sqlite3.connect(path)
                    try:
                        for version in range(1, 30):
                            connection.executescript(getattr(migrations, f"MIGRATION_{version}"))
                        connection.execute("PRAGMA user_version=29")
                        connection.commit()
                    finally:
                        connection.close()
                if fail:
                    with patch.object(
                        migrations, "MIGRATION_30", migrations.MIGRATION_30 + "\nINVALID SQL;"
                    ):
                        with self.assertRaises(sqlite3.DatabaseError):
                            migrations.migrate_database(path)
                else:
                    result = migrations.migrate_database(path)
                    self.assertEqual(result.current_version, 30)
                connection = sqlite3.connect(path)
                try:
                    self.assertEqual(
                        connection.execute("pragma user_version").fetchone()[0], 29 if fail else 30
                    )
                    self.assertEqual(
                        connection.execute("pragma integrity_check").fetchone()[0], "ok"
                    )
                    self.assertEqual(
                        connection.execute(
                            "select count(*) from sqlite_master where type='trigger' and name like 'codex_origin_%'"
                        ).fetchone()[0],
                        4,
                    )
                finally:
                    connection.close()
