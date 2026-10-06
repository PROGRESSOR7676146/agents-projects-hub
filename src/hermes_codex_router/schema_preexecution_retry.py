"""Additive preparation-retry provenance; never an accepted-turn continuation."""

PREEXECUTION_RETRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_preexecution_retry_tickets (
    source_job_id TEXT PRIMARY KEY REFERENCES provider_jobs(job_id),
    project_id TEXT NOT NULL,
    canonical_root TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    session_generation INTEGER NOT NULL,
    expected_thread_id TEXT,
    model TEXT NOT NULL,
    effort TEXT NOT NULL,
    codex_permission_profile TEXT,
    model_provider TEXT,
    payload_text TEXT NOT NULL,
    context_watermark INTEGER,
    handoff_id TEXT,
    source_inputs_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS provider_preexecution_retry_ticket_immutable
BEFORE UPDATE ON provider_preexecution_retry_tickets
BEGIN SELECT RAISE(ABORT, 'preparation retry ticket is immutable'); END;
CREATE TABLE IF NOT EXISTS provider_preexecution_retries (
    source_job_id TEXT PRIMARY KEY REFERENCES provider_preexecution_retry_tickets(source_job_id),
    child_job_id TEXT NOT NULL UNIQUE REFERENCES provider_jobs(job_id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_preexecution_retry_controls (
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    notice_message_id INTEGER NOT NULL,
    source_job_id TEXT NOT NULL REFERENCES provider_preexecution_retries(source_job_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(chat_id,message_id)
);
"""
