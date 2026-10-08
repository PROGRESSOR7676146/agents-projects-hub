"""Remove post-v24 structures when constructing fictional older test databases."""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from hermes_codex_router import migrations
from hermes_codex_router.schema_codex_permissions import (
    PROFILE_TABLES,
    ensure_codex_permission_columns,
)


@contextmanager
def legacy_delivery_hold_schema(connection: sqlite3.Connection):
    """Current FIFO queries need an empty schema44 table during old fixture seeding.

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
    try:
        yield
    finally:
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
        "telegram_delivery_hold_dispositions",
        "provider_recovery_notice_parts",
        "task_lifecycle_legacy_stop_links",
        "task_lifecycle_legacy_parts",
        "task_lifecycle_legacy_outbox",
        "task_lifecycle_notices",
    )
    existing = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    # Check every table before dropping any: a fixture must never discard evidence.
    for table in tables:
        if table in existing:
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
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
