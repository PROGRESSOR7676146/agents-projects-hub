"""Schema 44: immutable local owner permission to continue past delivery uncertainty."""

DELIVERY_HOLD_SCHEMA = """
CREATE TABLE telegram_delivery_hold_dispositions (
    outbox_id TEXT PRIMARY KEY REFERENCES telegram_outbox(outbox_id),
    job_id TEXT NOT NULL UNIQUE REFERENCES provider_jobs(job_id),
    result_id TEXT REFERENCES provider_job_results(result_id),
    topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    topic_sequence INTEGER NOT NULL CHECK(topic_sequence > 0),
    project_id TEXT NOT NULL,
    execution_scope TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    session_generation INTEGER NOT NULL CHECK(session_generation > 0),
    sender_agent_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    thread_id INTEGER NOT NULL CHECK(thread_id > 0),
    action TEXT NOT NULL CHECK(action='continue_without_confirmed_delivery'),
    snapshot_version INTEGER NOT NULL CHECK(snapshot_version=1),
    snapshot TEXT NOT NULL CHECK(length(snapshot)=64 AND snapshot NOT GLOB '*[^0-9a-f]*'),
    authority TEXT NOT NULL CHECK(authority='local_owner_cli'),
    applied_at TEXT NOT NULL
);
CREATE TRIGGER telegram_delivery_hold_dispositions_no_update
BEFORE UPDATE ON telegram_delivery_hold_dispositions
BEGIN SELECT RAISE(ABORT, 'delivery hold dispositions are immutable'); END;
CREATE TRIGGER telegram_delivery_hold_dispositions_no_delete
BEFORE DELETE ON telegram_delivery_hold_dispositions
BEGIN SELECT RAISE(ABORT, 'delivery hold dispositions are immutable'); END;
"""
