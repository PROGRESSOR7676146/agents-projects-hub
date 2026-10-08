"""Additive process observations; no native turn identity or approval authority."""

CLAUDE_ACTIVITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS claude_activity_observations (
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
    native_session_id TEXT NOT NULL,
    project_root TEXT NOT NULL,
    provider_started_at TEXT NOT NULL,
    permission_launch_epoch TEXT REFERENCES claude_permission_launches(launch_epoch),
    process_observed_at TEXT NOT NULL,
    retired_at TEXT,
    last_visible_sequence INTEGER NOT NULL DEFAULT 0 CHECK(last_visible_sequence>=0),
    quiet_since_at TEXT NOT NULL,
    permission_snapshot_digest TEXT NOT NULL CHECK(length(permission_snapshot_digest)=64),
    permission_waiting INTEGER NOT NULL CHECK(permission_waiting IN (0,1)),
    episode INTEGER NOT NULL DEFAULT 0 CHECK(episode>=0),
    notified_episode INTEGER CHECK(notified_episode IS NULL OR
        (notified_episode>=0 AND notified_episode<=episode))
);
"""
