"""Schema 48: immutable accepted targets and persistent control send ownership."""

CODEX_TURN_CONTROLS_SCHEMA = """
CREATE TABLE codex_turn_controls (
    job_id TEXT PRIMARY KEY REFERENCES provider_execution_checkpoints(job_id),
    origin TEXT NOT NULL CHECK(origin IN ('accepted_v48','legacy_read_only')),
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    session_generation INTEGER NOT NULL CHECK(session_generation > 0),
    topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    agent_id TEXT NOT NULL CHECK(length(agent_id) BETWEEN 1 AND 64),
    project_id TEXT NOT NULL CHECK(length(project_id) BETWEEN 1 AND 128),
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    input_chat_id INTEGER NOT NULL CHECK(input_chat_id != 0),
    thread_id INTEGER NOT NULL CHECK(thread_id >= 0),
    execution_scope TEXT NOT NULL,
    provider_thread_id TEXT NOT NULL CHECK(length(provider_thread_id) BETWEEN 1 AND 256),
    provider_turn_id TEXT NOT NULL CHECK(length(provider_turn_id) BETWEEN 1 AND 256),
    project_root TEXT NOT NULL CHECK(length(project_root) BETWEEN 1 AND 4096
        AND substr(project_root,1,1)='/' AND instr(project_root,char(0))=0),
    codex_permission_profile TEXT,
    accepted_at TEXT CHECK(origin='legacy_read_only' OR accepted_at IS NOT NULL),
    stop_request_id TEXT REFERENCES provider_stop_requests(request_id),
    late_read_attempts INTEGER NOT NULL DEFAULT 0 CHECK(late_read_attempts BETWEEN 0 AND 3),
    next_late_read_at TEXT,
    read_claim_token TEXT,
    read_claim_owner TEXT,
    read_claim_expires_at TEXT,
    send_owner_token_hash TEXT,
    send_started_at TEXT,
    interrupt_source TEXT CHECK(interrupt_source IN ('live','protective','late','permission_drift')),
    interrupt_outcome TEXT CHECK(interrupt_outcome IN ('matched_ack','matched_rejection','not_sent','unknown')),
    owner_quiesced_at TEXT,
    CHECK((send_started_at IS NULL AND send_owner_token_hash IS NULL AND interrupt_source IS NULL)
        OR (send_started_at IS NOT NULL AND send_owner_token_hash IS NOT NULL
            AND interrupt_source IS NOT NULL AND origin='accepted_v48')),
    CHECK(owner_quiesced_at IS NULL OR (send_started_at IS NOT NULL AND interrupt_outcome IS NOT NULL
        AND interrupt_outcome IN ('matched_ack','matched_rejection','not_sent'))),
    CHECK(interrupt_outcome IS NULL OR send_started_at IS NOT NULL),
    CHECK((read_claim_token IS NULL AND read_claim_owner IS NULL AND read_claim_expires_at IS NULL)
        OR (read_claim_token IS NOT NULL AND read_claim_owner IS NOT NULL
            AND read_claim_expires_at IS NOT NULL))
);
CREATE INDEX codex_turn_controls_due
ON codex_turn_controls(next_late_read_at,late_read_attempts);
CREATE UNIQUE INDEX codex_turn_controls_fresh_exact_target
ON codex_turn_controls(provider_thread_id,provider_turn_id) WHERE origin='accepted_v48';
CREATE INDEX codex_turn_controls_root_owner
ON codex_turn_controls(project_root) WHERE send_started_at IS NOT NULL AND owner_quiesced_at IS NULL;
CREATE TRIGGER codex_turn_controls_target_immutable
BEFORE UPDATE ON codex_turn_controls
WHEN NEW.job_id IS NOT OLD.job_id OR NEW.origin IS NOT OLD.origin
 OR NEW.session_id IS NOT OLD.session_id OR NEW.session_generation IS NOT OLD.session_generation
 OR NEW.topic_id IS NOT OLD.topic_id OR NEW.agent_id IS NOT OLD.agent_id
 OR NEW.project_id IS NOT OLD.project_id OR NEW.chat_id IS NOT OLD.chat_id
 OR NEW.input_chat_id IS NOT OLD.input_chat_id
 OR NEW.thread_id IS NOT OLD.thread_id OR NEW.execution_scope IS NOT OLD.execution_scope
 OR NEW.provider_thread_id IS NOT OLD.provider_thread_id
 OR NEW.provider_turn_id IS NOT OLD.provider_turn_id OR NEW.project_root IS NOT OLD.project_root
 OR NEW.codex_permission_profile IS NOT OLD.codex_permission_profile
 OR NEW.accepted_at IS NOT OLD.accepted_at
BEGIN SELECT RAISE(ABORT, 'accepted control target is immutable'); END;
CREATE TRIGGER codex_turn_controls_send_immutable
BEFORE UPDATE ON codex_turn_controls
WHEN (OLD.send_started_at IS NOT NULL AND
      (NEW.send_started_at IS NOT OLD.send_started_at OR NEW.send_owner_token_hash IS NOT OLD.send_owner_token_hash
       OR NEW.interrupt_source IS NOT OLD.interrupt_source))
 OR (OLD.owner_quiesced_at IS NOT NULL AND NEW.owner_quiesced_at IS NOT OLD.owner_quiesced_at)
 OR (OLD.stop_request_id IS NOT NULL AND NEW.stop_request_id IS NOT OLD.stop_request_id)
 OR (OLD.interrupt_outcome IN ('matched_ack','matched_rejection','not_sent')
     AND NEW.interrupt_outcome IS NOT OLD.interrupt_outcome)
 OR (OLD.interrupt_outcome='unknown' AND NEW.interrupt_outcome IS NULL)
 OR (OLD.next_late_read_at IS NOT NULL AND
     (NEW.next_late_read_at IS NULL OR NEW.next_late_read_at < OLD.next_late_read_at))
 OR NEW.late_read_attempts < OLD.late_read_attempts
BEGIN SELECT RAISE(ABORT, 'control authority cannot be reset'); END;
CREATE TRIGGER codex_turn_controls_no_delete
BEFORE DELETE ON codex_turn_controls
BEGIN SELECT RAISE(ABORT, 'control targets are retained'); END;

INSERT INTO codex_turn_controls
 (job_id,origin,session_id,session_generation,topic_id,agent_id,project_id,chat_id,input_chat_id,thread_id,
  execution_scope,provider_thread_id,provider_turn_id,project_root,codex_permission_profile,accepted_at)
SELECT checkpoint.job_id,'legacy_read_only',job.session_id,job.session_generation,
 job.topic_id,job.agent_id,topic.project_id,topic.chat_id,job.chat_id,topic.thread_id,
 COALESCE(topic.execution_scope,'project:' || topic.project_id),checkpoint.provider_thread_id,
 checkpoint.provider_turn_id,checkpoint.project_root,checkpoint.codex_permission_profile,NULL
FROM provider_execution_checkpoints checkpoint
JOIN provider_jobs job ON job.job_id=checkpoint.job_id JOIN topics topic ON topic.topic_id=job.topic_id
WHERE length(checkpoint.provider_thread_id) BETWEEN 1 AND 256
 AND length(checkpoint.provider_turn_id) BETWEEN 1 AND 256
 AND length(checkpoint.project_root) BETWEEN 1 AND 4096
 AND substr(checkpoint.project_root,1,1)='/' AND instr(checkpoint.project_root,char(0))=0;

ALTER TABLE hub_blocker_outbox ADD COLUMN control_job_id TEXT REFERENCES codex_turn_controls(job_id);
ALTER TABLE hub_blocker_outbox ADD COLUMN control_scope_error TEXT
    CHECK(control_scope_error IN ('unresolved','ambiguous'));
ALTER TABLE provider_job_holds ADD COLUMN control_job_id TEXT REFERENCES codex_turn_controls(job_id);
"""
