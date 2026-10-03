"""Remove post-v24 structures when constructing fictional older test databases."""

import sqlite3


def remove_task_lifecycle_schema(connection: sqlite3.Connection) -> None:
    """Remove empty schema-36 structures from fictional historical fixtures."""
    tables = (
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
    for table in tables:
        if table in existing:
            connection.execute(f"DROP TABLE {table}")


def remove_adoption_schema(connection: sqlite3.Connection) -> None:
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
