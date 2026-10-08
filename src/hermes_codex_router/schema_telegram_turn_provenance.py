"""Schema50: immutable admission provenance and fresh targets, without control."""

TELEGRAM_TURN_PROVENANCE_SCHEMA = """
CREATE TABLE provider_job_telegram_ingress (
    job_id TEXT PRIMARY KEY REFERENCES provider_jobs(job_id),
    ingress_identity TEXT NOT NULL CHECK(ingress_identity IN ('hub','codex')),
    UNIQUE(job_id,ingress_identity)
);
CREATE TABLE codex_telegram_precaution_targets (
    job_id TEXT PRIMARY KEY REFERENCES codex_turn_controls(job_id),
    ingress_identity TEXT NOT NULL CHECK(ingress_identity IN ('hub','codex')),
    FOREIGN KEY(job_id,ingress_identity)
        REFERENCES provider_job_telegram_ingress(job_id,ingress_identity)
);
CREATE TRIGGER provider_job_telegram_ingress_new_only
BEFORE INSERT ON provider_job_telegram_ingress
WHEN EXISTS(SELECT 1 FROM provider_job_telegram_ingress
    WHERE job_id=NEW.job_id OR rowid=NEW.rowid)
 OR NOT EXISTS(SELECT 1 FROM provider_jobs job WHERE job.job_id=NEW.job_id
    AND job.status='queued' AND job.attempt_count=0 AND job.lease_token IS NULL
    AND job.lease_owner IS NULL AND job.lease_expires_at IS NULL
    AND job.provider_started_at IS NULL
    AND NOT EXISTS(SELECT 1 FROM provider_job_inputs input WHERE input.job_id=job.job_id)
    AND NOT EXISTS(SELECT 1 FROM provider_execution_checkpoints p WHERE p.job_id=job.job_id))
BEGIN SELECT RAISE(ABORT, 'Telegram ingress requires new admission'); END;
CREATE TRIGGER provider_job_telegram_ingress_no_update
BEFORE UPDATE ON provider_job_telegram_ingress
BEGIN SELECT RAISE(ABORT, 'Telegram ingress is immutable'); END;
CREATE TRIGGER provider_job_telegram_ingress_no_delete
BEFORE DELETE ON provider_job_telegram_ingress
BEGIN SELECT RAISE(ABORT, 'Telegram ingress is retained'); END;
CREATE TRIGGER codex_telegram_precaution_target_fresh_only
BEFORE INSERT ON codex_telegram_precaution_targets
WHEN EXISTS(SELECT 1 FROM codex_telegram_precaution_targets
    WHERE job_id=NEW.job_id OR rowid=NEW.rowid)
 OR NOT EXISTS(SELECT 1 FROM codex_turn_controls control
    JOIN provider_execution_checkpoints p ON p.job_id=control.job_id
    JOIN provider_job_telegram_ingress ingress ON ingress.job_id=control.job_id
    WHERE control.job_id=NEW.job_id AND control.origin='accepted_v48'
      AND ingress.ingress_identity=NEW.ingress_identity AND p.provider_turn_id IS NULL
      AND p.provider_thread_id=control.provider_thread_id
      AND p.project_root=control.project_root
      AND p.codex_permission_profile IS control.codex_permission_profile)
BEGIN SELECT RAISE(ABORT, 'Telegram target requires fresh coherent acceptance'); END;
CREATE TRIGGER codex_telegram_precaution_target_no_update
BEFORE UPDATE ON codex_telegram_precaution_targets
BEGIN SELECT RAISE(ABORT, 'Telegram target is immutable'); END;
CREATE TRIGGER codex_telegram_precaution_target_no_delete
BEFORE DELETE ON codex_telegram_precaution_targets
BEGIN SELECT RAISE(ABORT, 'Telegram target is retained'); END;
CREATE TRIGGER provider_job_telegram_identity_fence
BEFORE UPDATE ON provider_jobs
WHEN EXISTS(SELECT 1 FROM provider_job_telegram_ingress WHERE job_id=OLD.job_id)
 AND (NEW.job_id IS NOT OLD.job_id OR NEW.idempotency_key IS NOT OLD.idempotency_key
   OR NEW.rowid IS NOT OLD.rowid
   OR NEW.chat_id IS NOT OLD.chat_id OR NEW.message_id IS NOT OLD.message_id
   OR NEW.topic_id IS NOT OLD.topic_id OR NEW.topic_sequence IS NOT OLD.topic_sequence
   OR NEW.agent_id IS NOT OLD.agent_id OR NEW.session_id IS NOT OLD.session_id
   OR NEW.session_generation IS NOT OLD.session_generation)
BEGIN SELECT RAISE(ABORT, 'Telegram job identity is immutable'); END;
CREATE TRIGGER provider_job_telegram_no_replace
BEFORE INSERT ON provider_jobs
WHEN EXISTS(SELECT 1 FROM provider_jobs job JOIN provider_job_telegram_ingress ingress
    ON ingress.job_id=job.job_id WHERE job.job_id=NEW.job_id OR job.rowid=NEW.rowid
    OR job.idempotency_key=NEW.idempotency_key
    OR (job.chat_id=NEW.chat_id AND job.message_id=NEW.message_id)
    OR (job.topic_id=NEW.topic_id AND job.topic_sequence=NEW.topic_sequence))
BEGIN SELECT RAISE(ABORT, 'Telegram job cannot be replaced'); END;
CREATE TRIGGER provider_job_telegram_no_update_replace
BEFORE UPDATE ON provider_jobs
WHEN EXISTS(SELECT 1 FROM provider_jobs job JOIN provider_job_telegram_ingress ingress
    ON ingress.job_id=job.job_id WHERE job.rowid != OLD.rowid
    AND (job.job_id=NEW.job_id OR job.rowid=NEW.rowid
      OR job.idempotency_key=NEW.idempotency_key
      OR (job.chat_id=NEW.chat_id AND job.message_id=NEW.message_id)
      OR (job.topic_id=NEW.topic_id AND job.topic_sequence=NEW.topic_sequence)))
BEGIN SELECT RAISE(ABORT, 'Telegram job cannot be replaced'); END;
CREATE TRIGGER provider_job_first_input_no_delete
BEFORE DELETE ON provider_job_inputs WHEN OLD.part_index=1
BEGIN SELECT RAISE(ABORT, 'First input admission closure is retained'); END;
CREATE TRIGGER provider_job_first_input_identity_fence
BEFORE UPDATE ON provider_job_inputs WHEN OLD.part_index=1
 AND (NEW.rowid IS NOT OLD.rowid OR NEW.job_id IS NOT OLD.job_id OR NEW.chat_id IS NOT OLD.chat_id
   OR NEW.message_id IS NOT OLD.message_id OR NEW.part_index IS NOT OLD.part_index)
BEGIN SELECT RAISE(ABORT, 'First input admission closure is immutable'); END;
CREATE TRIGGER provider_job_first_input_no_replace
BEFORE INSERT ON provider_job_inputs
WHEN EXISTS(SELECT 1 FROM provider_job_inputs
    WHERE part_index=1 AND (rowid=NEW.rowid OR (NEW.part_index=1 AND job_id=NEW.job_id)
      OR (chat_id=NEW.chat_id AND message_id=NEW.message_id)))
BEGIN SELECT RAISE(ABORT, 'First input admission closure cannot be replaced'); END;
CREATE TRIGGER provider_job_first_input_no_update_replace
BEFORE UPDATE ON provider_job_inputs
WHEN EXISTS(SELECT 1 FROM provider_job_inputs
    WHERE part_index=1 AND rowid != OLD.rowid
      AND (rowid=NEW.rowid OR (NEW.part_index=1 AND job_id=NEW.job_id)
        OR (chat_id=NEW.chat_id AND message_id=NEW.message_id)))
BEGIN SELECT RAISE(ABORT, 'First input admission closure cannot be replaced'); END;
CREATE TRIGGER codex_turn_controls_no_replace
BEFORE INSERT ON codex_turn_controls
WHEN EXISTS(SELECT 1 FROM codex_turn_controls control
    WHERE control.job_id=NEW.job_id OR control.rowid=NEW.rowid
      OR (control.origin='accepted_v48' AND NEW.origin='accepted_v48'
        AND control.provider_thread_id=NEW.provider_thread_id
        AND control.provider_turn_id=NEW.provider_turn_id))
BEGIN SELECT RAISE(ABORT, 'Retained control target cannot be replaced'); END;
CREATE TRIGGER codex_turn_controls_rowid_immutable
BEFORE UPDATE ON codex_turn_controls WHEN NEW.rowid IS NOT OLD.rowid
BEGIN SELECT RAISE(ABORT, 'Retained control row identity is immutable'); END;
"""
