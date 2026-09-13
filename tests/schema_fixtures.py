"""Remove post-v24 structures when constructing fictional older test databases."""

import sqlite3


def remove_adoption_schema(connection: sqlite3.Connection) -> None:
    # This is not a downgrade mechanism: retained origins must never be deleted.
    assert connection.execute("SELECT COUNT(*) FROM codex_session_origins").fetchone()[0] == 0
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
