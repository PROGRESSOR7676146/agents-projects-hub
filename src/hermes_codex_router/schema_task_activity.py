"""Additive, payload-free accepted-turn activity schema."""

TASK_ACTIVITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_activity (
    job_id TEXT PRIMARY KEY REFERENCES provider_jobs(job_id),
    lease_token TEXT NOT NULL,
    topic_id INTEGER NOT NULL,
    project_id TEXT NOT NULL,
    execution_scope TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    session_generation INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    provider_thread_id TEXT NOT NULL,
    provider_turn_id TEXT NOT NULL,
    project_root TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('ordinary','tool','approval')),
    last_meaningful_at TEXT NOT NULL,
    episode INTEGER NOT NULL DEFAULT 0,
    notified_episode INTEGER
);
CREATE TABLE IF NOT EXISTS task_activity_entries (
    job_id TEXT NOT NULL REFERENCES task_activity(job_id),
    kind TEXT NOT NULL CHECK(kind IN ('tool','message','approval')),
    identity TEXT NOT NULL CHECK(length(identity)=64),
    category TEXT NOT NULL,
    item_identity TEXT,
    state TEXT NOT NULL CHECK(state IN ('active','completed','pending','resolved')),
    PRIMARY KEY(job_id,kind,identity)
);
"""
