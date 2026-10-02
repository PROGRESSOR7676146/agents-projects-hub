"""Additive task-control delivery schema; no provider invocation or runtime imports."""

TASK_LIFECYCLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_lifecycle_notices (
    notice_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    job_id TEXT REFERENCES provider_jobs(job_id),
    stop_request_id TEXT REFERENCES provider_stop_requests(request_id),
    chat_id INTEGER NOT NULL,
    thread_id INTEGER NOT NULL,
    reply_to_message_id INTEGER,
    telegram_html TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','leased','delivered','unknown','failed','superseded')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
    available_at TEXT NOT NULL,
    lease_token TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    send_started_at TEXT,
    telegram_message_id INTEGER CHECK(telegram_message_id > 0),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(job_id IS NOT NULL OR stop_request_id IS NOT NULL)
);
CREATE TABLE IF NOT EXISTS task_lifecycle_legacy_stop_links (
    stop_request_id TEXT PRIMARY KEY REFERENCES provider_stop_requests(request_id),
    notice_id TEXT NOT NULL REFERENCES task_lifecycle_notices(notice_id)
);
CREATE INDEX IF NOT EXISTS task_notice_due
ON task_lifecycle_notices(status, available_at, created_at);
CREATE INDEX IF NOT EXISTS task_notice_stop
ON task_lifecycle_notices(stop_request_id);
"""

# Freeze the released schema-35 coverage rule here. Migration code must not
# depend on a runtime state facade, whose query can evolve after this release.
_LEGACY_COVERAGE = """
job.job_id = outbox.job_id
AND topic.topic_id = job.topic_id
AND stop.topic_id = job.topic_id
AND stop.chat_id = outbox.chat_id
AND topic.chat_id = outbox.chat_id
AND topic.thread_id = outbox.thread_id
AND job.created_at <= stop.created_at
AND NOT EXISTS (
    SELECT 1 FROM provider_job_holds held
    WHERE held.job_id = job.job_id AND held.held_at <= stop.created_at
      AND (held.decision = 'pending' OR held.decided_at > stop.created_at)
)
"""

_ORIGINAL_NOTICE_MATCH = _LEGACY_COVERAGE + " AND stop.created_at <= outbox.created_at"
_DUPLICATE_NOTICE_MATCH = (
    _LEGACY_COVERAGE
    + """
AND (
    stop.created_at <= outbox.created_at
    OR (job.status = 'cancelled' AND job.error_class = 'user_stop'
        AND job.error_code = 'emergency_stop' AND stop.created_at <= job.updated_at)
    OR (job.status IN ('leased', 'executing') AND stop.status = 'pending')
)
"""
)

MIGRATION_36 = (
    TASK_LIFECYCLE_SCHEMA
    + f"""
ALTER TABLE provider_turn_terminal_evidence RENAME TO provider_turn_terminal_evidence_v35;
CREATE TABLE provider_turn_terminal_evidence (
    job_id TEXT PRIMARY KEY REFERENCES provider_jobs(job_id),
    terminal_status TEXT NOT NULL CHECK(terminal_status IN ('completed','failed','interrupted')),
    provider_thread_id TEXT NOT NULL,
    provider_turn_id TEXT NOT NULL,
    project_root TEXT NOT NULL,
    observed_at TEXT NOT NULL
);
INSERT INTO provider_turn_terminal_evidence SELECT * FROM provider_turn_terminal_evidence_v35;
DROP TABLE provider_turn_terminal_evidence_v35;

CREATE TABLE task_lifecycle_legacy_outbox AS SELECT * FROM telegram_outbox WHERE 0;
CREATE UNIQUE INDEX task_legacy_outbox_identity ON task_lifecycle_legacy_outbox(outbox_id);
CREATE TABLE task_lifecycle_legacy_parts AS SELECT * FROM telegram_outbox_parts WHERE 0;
CREATE UNIQUE INDEX task_legacy_part_identity ON task_lifecycle_legacy_parts(outbox_id,part_index);

CREATE TABLE task_lifecycle_migration_guard (
    unrecognized_hub_stop_rows INTEGER CHECK(unrecognized_hub_stop_rows = 0)
);
INSERT INTO task_lifecycle_migration_guard
SELECT COUNT(*) FROM telegram_outbox outbox
WHERE outbox.sender_agent_id = 'hub' AND NOT EXISTS (
    SELECT 1 FROM provider_jobs job JOIN topics topic ON topic.topic_id = job.topic_id
    JOIN provider_stop_requests stop ON stop.topic_id = job.topic_id
    WHERE {_ORIGINAL_NOTICE_MATCH}
);
DROP TABLE task_lifecycle_migration_guard;

INSERT INTO task_lifecycle_legacy_outbox SELECT * FROM telegram_outbox WHERE sender_agent_id = 'hub';
INSERT INTO task_lifecycle_legacy_parts
SELECT part.* FROM telegram_outbox_parts part
JOIN task_lifecycle_legacy_outbox outbox ON outbox.outbox_id = part.outbox_id;

INSERT INTO task_lifecycle_notices
(notice_id,event_key,kind,job_id,stop_request_id,chat_id,thread_id,reply_to_message_id,
 telegram_html,status,attempt_count,available_at,telegram_message_id,error_code,created_at,updated_at)
SELECT 'legacy-stop:' || outbox.outbox_id, 'legacy-stop:' || outbox.outbox_id,
       'legacy_stop', outbox.job_id,
       (SELECT stop.request_id FROM provider_jobs job
        JOIN topics topic ON topic.topic_id = job.topic_id
        JOIN provider_stop_requests stop ON stop.topic_id = job.topic_id
        WHERE {_ORIGINAL_NOTICE_MATCH}
        ORDER BY stop.created_at,stop.request_id LIMIT 1),
       outbox.chat_id,outbox.thread_id,NULL,outbox.telegram_html,
       CASE
         WHEN outbox.status = 'delivered'
          AND EXISTS (SELECT 1 FROM task_lifecycle_legacy_parts part
                      WHERE part.outbox_id = outbox.outbox_id)
          AND NOT EXISTS (SELECT 1 FROM task_lifecycle_legacy_parts part
                          WHERE part.outbox_id = outbox.outbox_id
                            AND part.telegram_message_id IS NULL)
         THEN 'delivered'
         WHEN outbox.status = 'failed' THEN 'failed'
         WHEN outbox.status = 'pending' AND outbox.attempt_count = 0
          AND outbox.telegram_message_id IS NULL
          AND outbox.lease_owner IS NULL AND outbox.lease_token IS NULL
          AND outbox.lease_expires_at IS NULL AND length(outbox.telegram_html) <= 3500
          AND (SELECT COUNT(*) FROM task_lifecycle_legacy_parts part
               WHERE part.outbox_id = outbox.outbox_id) = 1
          AND EXISTS (SELECT 1 FROM task_lifecycle_legacy_parts part
                      WHERE part.outbox_id = outbox.outbox_id
                        AND part.part_type = 'text' AND part.telegram_message_id IS NULL
                        AND part.telegram_html = outbox.telegram_html)
         THEN 'pending'
         ELSE 'unknown'
       END,
       outbox.attempt_count,outbox.available_at,
       CASE WHEN outbox.status = 'delivered'
          AND EXISTS (SELECT 1 FROM task_lifecycle_legacy_parts part
                      WHERE part.outbox_id = outbox.outbox_id)
          AND NOT EXISTS (SELECT 1 FROM task_lifecycle_legacy_parts part
                          WHERE part.outbox_id = outbox.outbox_id
                            AND part.telegram_message_id IS NULL)
       THEN (SELECT part.telegram_message_id FROM task_lifecycle_legacy_parts part
             WHERE part.outbox_id = outbox.outbox_id ORDER BY part.part_index DESC LIMIT 1)
       ELSE NULL END,
       'legacy_stop_delivery',outbox.created_at,outbox.updated_at
FROM task_lifecycle_legacy_outbox outbox;

INSERT INTO task_lifecycle_legacy_stop_links(stop_request_id,notice_id)
SELECT stop.request_id,
       (SELECT 'legacy-stop:' || outbox.outbox_id
        FROM task_lifecycle_legacy_outbox outbox
        JOIN provider_jobs job ON job.job_id = outbox.job_id
        JOIN topics topic ON topic.topic_id = job.topic_id
        WHERE {_DUPLICATE_NOTICE_MATCH}
        ORDER BY outbox.created_at,outbox.outbox_id LIMIT 1)
FROM provider_stop_requests stop
WHERE EXISTS (
    SELECT 1 FROM task_lifecycle_legacy_outbox outbox
    JOIN provider_jobs job ON job.job_id = outbox.job_id
    JOIN topics topic ON topic.topic_id = job.topic_id
    WHERE {_DUPLICATE_NOTICE_MATCH}
);

DELETE FROM telegram_outbox_parts
WHERE outbox_id IN (SELECT outbox_id FROM task_lifecycle_legacy_outbox);
DELETE FROM telegram_outbox
WHERE outbox_id IN (SELECT outbox_id FROM task_lifecycle_legacy_outbox);
"""
)
