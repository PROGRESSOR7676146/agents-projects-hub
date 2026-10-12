"""Schema45: append-only owner decisions and the existing notice sender's third subject."""

OUTCOME_ASSESSMENT_SCHEMA = """
CREATE TABLE outcome_assessment_dispositions (
    disposition_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    topic_id INTEGER REFERENCES topics(topic_id),
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    thread_id INTEGER NOT NULL CHECK(thread_id > 0),
    input_message_id INTEGER NOT NULL CHECK(input_message_id > 0),
    owner_user_id INTEGER NOT NULL CHECK(owner_user_id > 0),
    reply_message_id INTEGER CHECK(reply_message_id > 0),
    fingerprint_version INTEGER NOT NULL CHECK(fingerprint_version=1),
    input_fingerprint TEXT NOT NULL CHECK(length(input_fingerprint)=64),
    disposition TEXT NOT NULL CHECK(disposition IN ('applied','refused')),
    refusal_code TEXT CHECK(length(refusal_code) BETWEEN 1 AND 64),
    job_id TEXT REFERENCES provider_jobs(job_id),
    result_id TEXT REFERENCES provider_job_results(result_id),
    outbox_id TEXT REFERENCES telegram_outbox(outbox_id),
    decision TEXT CHECK(decision IN ('accepted','rework','unknown')),
    reason TEXT CHECK(length(reason) BETWEEN 1 AND 500),
    predecessor_id TEXT REFERENCES outcome_assessment_dispositions(disposition_id),
    revision INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(chat_id,input_message_id),
    CHECK((disposition='applied' AND refusal_code IS NULL AND topic_id IS NOT NULL
           AND job_id IS NOT NULL AND result_id IS NOT NULL AND outbox_id IS NOT NULL
           AND decision IS NOT NULL AND reason IS NOT NULL AND revision IS NOT NULL AND revision>0
           AND ((revision=1 AND predecessor_id IS NULL) OR
                (revision>1 AND predecessor_id IS NOT NULL)))
          OR (disposition='refused' AND refusal_code IS NOT NULL
              AND decision IS NULL AND reason IS NULL AND revision IS NULL
              AND predecessor_id IS NULL))
);
CREATE UNIQUE INDEX outcome_assessment_revision
ON outcome_assessment_dispositions(result_id,revision) WHERE disposition='applied';
CREATE UNIQUE INDEX outcome_assessment_successor
ON outcome_assessment_dispositions(predecessor_id)
WHERE disposition='applied' AND predecessor_id IS NOT NULL;
CREATE TRIGGER outcome_assessment_no_update BEFORE UPDATE ON outcome_assessment_dispositions
BEGIN SELECT RAISE(ABORT,'outcome assessment is append-only'); END;
CREATE TRIGGER outcome_assessment_no_delete BEFORE DELETE ON outcome_assessment_dispositions
BEGIN SELECT RAISE(ABORT,'outcome assessment is append-only'); END;

CREATE TABLE task_lifecycle_notices_m45 (
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
    assessment_disposition_id TEXT REFERENCES outcome_assessment_dispositions(disposition_id),
    CHECK(job_id IS NOT NULL OR stop_request_id IS NOT NULL OR assessment_disposition_id IS NOT NULL)
);
INSERT INTO task_lifecycle_notices_m45 (
    notice_id,event_key,kind,job_id,stop_request_id,chat_id,thread_id,reply_to_message_id,
    telegram_html,status,attempt_count,available_at,lease_token,lease_owner,lease_expires_at,
    send_started_at,telegram_message_id,error_code,created_at,updated_at,assessment_disposition_id
)
SELECT notice_id,event_key,kind,job_id,stop_request_id,chat_id,thread_id,reply_to_message_id,
    telegram_html,status,attempt_count,available_at,lease_token,lease_owner,lease_expires_at,
    send_started_at,telegram_message_id,error_code,created_at,updated_at,NULL
FROM task_lifecycle_notices;
CREATE TABLE task_lifecycle_legacy_stop_links_m45 (
    stop_request_id TEXT PRIMARY KEY REFERENCES provider_stop_requests(request_id),
    notice_id TEXT NOT NULL REFERENCES task_lifecycle_notices_m45(notice_id)
);
INSERT INTO task_lifecycle_legacy_stop_links_m45(stop_request_id,notice_id)
SELECT stop_request_id,notice_id FROM task_lifecycle_legacy_stop_links;
DROP TABLE task_lifecycle_legacy_stop_links;
DROP TABLE task_lifecycle_notices;
ALTER TABLE task_lifecycle_notices_m45 RENAME TO task_lifecycle_notices;
ALTER TABLE task_lifecycle_legacy_stop_links_m45 RENAME TO task_lifecycle_legacy_stop_links;
CREATE INDEX task_notice_due ON task_lifecycle_notices(status,available_at,created_at);
CREATE INDEX task_notice_stop ON task_lifecycle_notices(stop_request_id);
CREATE UNIQUE INDEX task_notice_assessment ON task_lifecycle_notices(assessment_disposition_id)
WHERE assessment_disposition_id IS NOT NULL;
"""
