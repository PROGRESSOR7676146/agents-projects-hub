"""Additive, payload-free custody of one Claude file-tool launch."""

CLAUDE_PERMISSIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS claude_permission_session_modes (
    provider_session_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK(mode IN ('text_only','file_tools')),
    home_digest TEXT NOT NULL CHECK(length(home_digest)=64)
);
CREATE TABLE IF NOT EXISTS claude_permission_launches (
    launch_epoch TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES provider_jobs(job_id),
    binding_digest TEXT NOT NULL CHECK(length(binding_digest)=64),
    status TEXT NOT NULL CHECK(status IN ('active','closed')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claude_permission_requests (
    request_nonce TEXT PRIMARY KEY,
    launch_epoch TEXT NOT NULL REFERENCES claude_permission_launches(launch_epoch),
    payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64),
    event_digest TEXT NOT NULL CHECK(length(event_digest)=64),
    expires_at INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','allow','deny','revoked')),
    consumed_at TEXT
);
CREATE INDEX IF NOT EXISTS claude_permission_requests_launch
ON claude_permission_requests(launch_epoch,status);
"""
