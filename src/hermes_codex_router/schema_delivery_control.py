"""Schema 46: storage prerequisite only; no runtime authority or apply API."""

DELIVERY_CONTROL_SCHEMA = """
CREATE TABLE telegram_delivery_control_dispositions (
    disposition_id TEXT NOT NULL PRIMARY KEY CHECK(length(disposition_id) BETWEEN 1 AND 128),
    target_kind TEXT NOT NULL CHECK(target_kind IN ('final_outbox','progress_delivery')),
    outbox_id TEXT UNIQUE REFERENCES telegram_outbox(outbox_id),
    progress_id TEXT UNIQUE REFERENCES provider_progress_deliveries(progress_id),
    item_sequence INTEGER REFERENCES provider_visible_items(sequence) CHECK(item_sequence > 0),
    job_id TEXT NOT NULL REFERENCES provider_jobs(job_id),
    result_id TEXT REFERENCES provider_job_results(result_id),
    topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    topic_sequence INTEGER NOT NULL CHECK(topic_sequence > 0),
    project_id TEXT NOT NULL CHECK(length(project_id) BETWEEN 1 AND 128),
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    session_generation INTEGER NOT NULL CHECK(session_generation > 0),
    sender_agent_id TEXT NOT NULL CHECK(length(sender_agent_id) BETWEEN 1 AND 64),
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    thread_id INTEGER NOT NULL CHECK(thread_id > 0),
    execution_scope_at_consent TEXT NOT NULL
        CHECK(length(execution_scope_at_consent) BETWEEN 6 AND 4101
              AND substr(execution_scope_at_consent,1,6)='root:/'),
    delivery_status_at_consent TEXT NOT NULL CHECK(delivery_status_at_consent IN ('unknown','failed')),
    action TEXT NOT NULL CHECK(action='reconcile_delivery_control_wait'),
    snapshot_version INTEGER NOT NULL CHECK(snapshot_version=1),
    snapshot TEXT NOT NULL CHECK(length(snapshot)=64 AND snapshot NOT GLOB '*[^0-9a-f]*'),
    authority TEXT NOT NULL CHECK(authority='local_owner_cli'),
    applied_at TEXT NOT NULL,
    terminal_evidence_job_id TEXT REFERENCES provider_turn_terminal_evidence(job_id)
        CHECK(terminal_evidence_job_id IS NULL OR terminal_evidence_job_id=job_id),
    resolution_job_id TEXT REFERENCES provider_job_resolutions(job_id)
        CHECK(resolution_job_id IS NULL OR resolution_job_id=job_id),
    CHECK(
        (target_kind='final_outbox' AND outbox_id IS NOT NULL
         AND progress_id IS NULL AND item_sequence IS NULL)
        OR
        (target_kind='progress_delivery' AND progress_id IS NOT NULL
         AND outbox_id IS NULL AND item_sequence IS NOT NULL AND result_id IS NULL)
    )
);
CREATE TRIGGER telegram_delivery_control_dispositions_no_update
BEFORE UPDATE ON telegram_delivery_control_dispositions
BEGIN SELECT RAISE(ABORT, 'delivery control dispositions are immutable'); END;
CREATE TRIGGER telegram_delivery_control_dispositions_no_delete
BEFORE DELETE ON telegram_delivery_control_dispositions
BEGIN SELECT RAISE(ABORT, 'delivery control dispositions are immutable'); END;
"""
