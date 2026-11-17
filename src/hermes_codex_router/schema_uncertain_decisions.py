"""Schema 57: the owner's Telegram decision for one uncertain provider job.

The existing resolution row still releases the root and keeps its meaning.
This ledger adds what that row cannot say: which button the configured owner
pressed, on which delivered Hub prompt, against which session snapshot, and
which inspection-first job it created. Rows are append-only.
"""

UNCERTAIN_DECISION_SCHEMA = """
CREATE TABLE provider_job_owner_decisions (
    job_id TEXT PRIMARY KEY REFERENCES provider_job_resolutions(job_id),
    decision TEXT NOT NULL CHECK(decision IN ('inspect_and_continue', 'close_without_retry')),
    authority TEXT NOT NULL CHECK(authority = 'telegram_owner_callback'),
    actor_user_id INTEGER NOT NULL CHECK(actor_user_id > 0),
    callback_id TEXT NOT NULL UNIQUE CHECK(length(callback_id) BETWEEN 1 AND 200),
    outbox_id TEXT NOT NULL CHECK(length(outbox_id) BETWEEN 1 AND 128),
    prompt_part_index INTEGER NOT NULL CHECK(prompt_part_index > 0),
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    thread_id INTEGER NOT NULL CHECK(thread_id > 0),
    prompt_message_id INTEGER NOT NULL CHECK(prompt_message_id > 0),
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    session_generation INTEGER NOT NULL CHECK(session_generation > 0),
    snapshot TEXT NOT NULL CHECK(length(snapshot) = 16 AND snapshot NOT GLOB '*[^0-9a-f]*'),
    continuation_job_id TEXT UNIQUE REFERENCES provider_jobs(job_id),
    decided_at TEXT NOT NULL,
    UNIQUE(chat_id, prompt_message_id),
    CHECK((decision = 'inspect_and_continue') = (continuation_job_id IS NOT NULL))
);
CREATE TRIGGER provider_job_owner_decisions_require_resolution
BEFORE INSERT ON provider_job_owner_decisions
WHEN NOT EXISTS (
    SELECT 1 FROM provider_job_resolutions resolution
    WHERE resolution.job_id = NEW.job_id AND resolution.resolution = 'acknowledged'
) OR (NEW.continuation_job_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM provider_job_continuations continuation
    WHERE continuation.source_job_id = NEW.job_id
      AND continuation.continuation_job_id = NEW.continuation_job_id
))
BEGIN SELECT RAISE(ABORT, 'owner decision needs its resolution and continuation'); END;
CREATE TRIGGER provider_job_owner_decisions_no_update
BEFORE UPDATE ON provider_job_owner_decisions
BEGIN SELECT RAISE(ABORT, 'owner decisions are immutable'); END;
CREATE TRIGGER provider_job_owner_decisions_no_delete
BEFORE DELETE ON provider_job_owner_decisions
BEGIN SELECT RAISE(ABORT, 'owner decisions cannot be deleted'); END;
"""
