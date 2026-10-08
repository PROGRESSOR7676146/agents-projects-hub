"""Remove post-v24 structures when constructing fictional older test databases."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router import migrations
from hermes_codex_router.schema_codex_permissions import (
    PROFILE_TABLES,
    ensure_codex_permission_columns,
)


def project_historical_database(source: Path, target: Path, version: int) -> None:
    """Build a separate genuine old-schema fixture; retain the source authority.

    Test data only. This is neither a runtime downgrade nor a recovery operation.
    """
    assert not target.exists()
    with patch.object(migrations, "LATEST_SCHEMA_VERSION", version):
        migrations.migrate_database(target, create_backup=False)
    with closing(sqlite3.connect(source)) as current, closing(sqlite3.connect(target)) as old:
        tables = [
            row[0]
            for row in old.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        for table in tables:
            names = [row[1] for row in old.execute(f'PRAGMA table_info("{table}")')]
            quoted = ",".join(f'"{name}"' for name in names)
            rows = current.execute(f'SELECT {quoted} FROM "{table}"').fetchall()
            if rows:
                old.executemany(
                    f'INSERT INTO "{table}" ({quoted}) VALUES ({",".join("?" for _ in names)})',
                    rows,
                )
        old.commit()
        assert old.execute("PRAGMA foreign_key_check").fetchall() == []


@contextmanager
def legacy_delivery_hold_schema(connection: sqlite3.Connection):
    """Current FIFO queries need empty delivery ledgers during old fixture seeding.

    Only a temporary, fictional compatibility bridge; remove before migration or
    backup assertions. It never licenses dropping retained owner evidence.
    """
    created = (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='telegram_delivery_hold_dispositions'"
        ).fetchone()
        is None
    )
    if created:
        with connection:
            migrations._execute_migration_script(connection, migrations.MIGRATION_44)
    control_created = (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='telegram_delivery_control_dispositions'"
        ).fetchone()
        is None
    )
    if control_created:
        with connection:
            migrations._execute_migration_script(connection, migrations.MIGRATION_46)
    try:
        yield
    finally:
        if control_created:
            with connection:
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM telegram_delivery_control_dispositions"
                    ).fetchone()[0]
                    == 0
                )
                connection.execute("DROP TABLE telegram_delivery_control_dispositions")
        if created:
            with connection:
                assert (
                    connection.execute(
                        "SELECT COUNT(*) FROM telegram_delivery_hold_dispositions"
                    ).fetchone()[0]
                    == 0
                )
                connection.execute("DROP TRIGGER telegram_delivery_hold_topic_binding_guard")
                connection.execute("DROP TABLE telegram_delivery_hold_dispositions")


@contextmanager
def legacy_selection_columns(path: Path):
    """Temporarily support current fixture builders, then restore historical DDL.

    Current state facades require schema-39 columns. Only fictional all-null
    fixture rows may use this bridge; real upgrades and backups see old DDL.
    """
    migrations.migrate_database(path, create_backup=False)
    with closing(sqlite3.connect(path)) as connection, connection:
        ensure_codex_permission_columns(connection)
    try:
        with closing(sqlite3.connect(path)) as bridge, legacy_delivery_hold_schema(bridge):
            yield
    finally:
        with closing(sqlite3.connect(path)) as connection, connection:
            for table in PROFILE_TABLES:
                assert (
                    connection.execute(
                        f"SELECT count(*) FROM {table} WHERE codex_permission_profile IS NOT NULL"
                    ).fetchone()[0]
                    == 0
                )
                connection.execute(f"ALTER TABLE {table} DROP COLUMN codex_permission_profile")


def remove_task_lifecycle_schema(connection: sqlite3.Connection) -> None:
    """Remove empty lifecycle/archive structures from fictional historical fixtures."""
    tables = (
        "codex_telegram_precaution_targets",
        "provider_job_telegram_ingress",
        "telegram_group_ingress",
        "codex_turn_controls",
        "telegram_delivery_control_dispositions",
        "telegram_delivery_hold_dispositions",
        "provider_recovery_notice_parts",
        "task_lifecycle_legacy_stop_links",
        "task_lifecycle_legacy_parts",
        "task_lifecycle_legacy_outbox",
        "task_lifecycle_notices",
        "outcome_assessment_dispositions",
    )
    existing = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    # Check every table before dropping any: a fixture must never discard evidence.
    for table in tables:
        if table in existing:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    for trigger in (
        "provider_job_telegram_identity_fence",
        "provider_job_telegram_no_replace",
        "provider_job_telegram_no_update_replace",
        "provider_job_first_input_no_delete",
        "provider_job_first_input_identity_fence",
        "provider_job_first_input_no_replace",
        "provider_job_first_input_no_update_replace",
    ):
        connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    if "codex_turn_controls" in existing:
        connection.execute("ALTER TABLE hub_blocker_outbox DROP COLUMN control_scope_error")
        for table in ("hub_blocker_outbox", "provider_job_holds"):
            connection.execute(f"ALTER TABLE {table} DROP COLUMN control_job_id")
    connection.execute("DROP TRIGGER IF EXISTS telegram_delivery_hold_topic_binding_guard")
    for table in tables:
        if table in existing:
            connection.execute(f"DROP TABLE {table}")


def remove_adoption_schema(connection: sqlite3.Connection) -> None:
    # A historical fixture must not keep new cross-table snapshot guards when
    # its provider_jobs table is reconstructed with historical columns.
    for table in PROFILE_TABLES:
        connection.execute(f"DROP TRIGGER IF EXISTS {table}_codex_profile_immutable")
    connection.execute("DROP TRIGGER IF EXISTS provider_jobs_codex_profile_snapshot")
    connection.execute(
        "DROP TRIGGER IF EXISTS provider_execution_checkpoints_codex_profile_snapshot"
    )
    remove_task_lifecycle_schema(connection)
    # This is not a downgrade mechanism: retained origins must never be deleted.
    assert connection.execute("SELECT COUNT(*) FROM codex_session_origins").fetchone()[0] == 0
    connection.execute("DROP TABLE project_edit_root_options")
    connection.execute("DROP TABLE project_edit_project_options")
    connection.execute("DROP TABLE project_edit_workflows")
    connection.execute("DROP TABLE project_command_cooldowns")
    connection.execute("DROP TABLE project_command_scopes")
    connection.execute("DROP TABLE project_onboarding_outbox")
    connection.execute("DROP TABLE project_group_bindings")
    connection.execute("DROP TABLE project_onboarding_options")
    connection.execute("DROP TABLE project_onboarding_workflows")
    connection.execute("DROP TABLE session_connect_outbox")
    connection.execute("DROP TABLE session_connect_options")
    connection.execute("DROP TABLE session_connect_candidates")
    connection.execute("DROP TABLE session_connect_workflows")
    connection.execute("DROP TABLE session_connect_codes")
    connection.execute("DROP TABLE session_connect_code_attempts")
    connection.execute("DROP TRIGGER codex_origin_binding_guard")
    connection.execute("DROP TABLE codex_session_origins")
    connection.execute("ALTER TABLE external_turn_excerpts DROP COLUMN source_message_id")
