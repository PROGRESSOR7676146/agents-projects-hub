"""Additive passive approval observations; never an execution checkpoint."""

PREACCEPTANCE_APPROVAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS preacceptance_runtime_epochs (
    slot_key TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    worker_slot INTEGER NOT NULL CHECK(worker_slot BETWEEN 1 AND 16),
    epoch INTEGER NOT NULL CHECK(epoch > 0),
    instance_token TEXT NOT NULL,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS preacceptance_scopes (
    scope_id TEXT PRIMARY KEY,
    slot_key TEXT NOT NULL REFERENCES preacceptance_runtime_epochs(slot_key),
    epoch INTEGER NOT NULL,
    instance_token TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES provider_jobs(job_id),
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
    project_root TEXT NOT NULL,
    codex_permission_profile TEXT,
    state TEXT NOT NULL CHECK(state IN ('open','promoted','retired')),
    created_at TEXT NOT NULL,
    closed_at TEXT,
    UNIQUE(job_id,lease_token)
);
CREATE UNIQUE INDEX IF NOT EXISTS preacceptance_one_open_scope_per_slot
ON preacceptance_scopes(slot_key) WHERE state='open';
CREATE TABLE IF NOT EXISTS preacceptance_requests (
    scope_id TEXT NOT NULL REFERENCES preacceptance_scopes(scope_id),
    identity TEXT NOT NULL CHECK(length(identity)=64),
    observed_turn_id TEXT NOT NULL CHECK(length(observed_turn_id) BETWEEN 1 AND 256),
    item_identity TEXT NOT NULL CHECK(length(item_identity)=64),
    category TEXT NOT NULL CHECK(category IN ('command','file_change','network','permissions')),
    state TEXT NOT NULL CHECK(state IN ('pending','resolved','retired')),
    event_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    PRIMARY KEY(scope_id,identity)
);
"""
