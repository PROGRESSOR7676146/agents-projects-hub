"""Schema52: first-send ingress provenance, never historical enrichment."""

INGRESS_CONTROL_SCHEMA = """
ALTER TABLE codex_turn_controls ADD COLUMN ingress_assessment_revision_at_send INTEGER
    CHECK(ingress_assessment_revision_at_send IS NULL OR
      (typeof(ingress_assessment_revision_at_send)='integer'
       AND ingress_assessment_revision_at_send BETWEEN 1 AND 9223372036854775807));

CREATE TABLE codex_ingress_interrupt_causes (
    job_id TEXT PRIMARY KEY NOT NULL REFERENCES codex_telegram_precaution_targets(job_id),
    assessment_revision INTEGER NOT NULL CHECK(assessment_revision>0),
    policy_version INTEGER NOT NULL CHECK(policy_version=1),
    episode_generation INTEGER NOT NULL CHECK(episode_generation>0),
    reason TEXT NOT NULL CHECK(reason IN ('never_confirmed','stale_or_missing','poll_failures')),
    since TEXT NOT NULL,
    deadline TEXT NOT NULL,
    recovery_after TEXT NOT NULL,
    recovery_cutoff_epoch INTEGER,
    recovery_cutoff_sequence INTEGER,
    source_failure_epoch INTEGER,
    source_failure_sequence INTEGER
);

CREATE TRIGGER codex_ingress_send_no_insert_enrichment
BEFORE INSERT ON codex_turn_controls
WHEN NEW.ingress_assessment_revision_at_send IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'ingress provenance requires a first send transition'); END;

CREATE TRIGGER codex_ingress_send_first_transition
BEFORE UPDATE ON codex_turn_controls
WHEN NEW.ingress_assessment_revision_at_send IS NOT OLD.ingress_assessment_revision_at_send
 AND (OLD.send_started_at IS NOT NULL OR OLD.ingress_assessment_revision_at_send IS NOT NULL
      OR NEW.ingress_assessment_revision_at_send IS NULL
      OR NEW.send_started_at IS NULL OR NEW.origin!='accepted_v48'
      OR NEW.interrupt_source NOT IN ('protective','late')
      OR NOT EXISTS (
          SELECT 1 FROM codex_telegram_ingress_assessments assessment
          JOIN codex_telegram_precaution_targets target ON target.job_id=assessment.job_id
          WHERE assessment.job_id=NEW.job_id
            AND assessment.assessment_revision=NEW.ingress_assessment_revision_at_send
            AND assessment.last_assessed_at=NEW.send_started_at
            AND assessment.reason IS NOT NULL AND assessment.recent_poll_confirmed=0
            AND assessment.deadline<=NEW.send_started_at))
BEGIN SELECT RAISE(ABORT, 'ingress send provenance cannot be enriched or reset'); END;

CREATE TRIGGER codex_ingress_cause_no_replace
BEFORE INSERT ON codex_ingress_interrupt_causes
WHEN EXISTS(SELECT 1 FROM codex_ingress_interrupt_causes
            WHERE job_id=NEW.job_id OR rowid=NEW.rowid)
 OR NOT EXISTS (
    SELECT 1 FROM codex_turn_controls control
    JOIN codex_telegram_ingress_assessments assessment ON assessment.job_id=control.job_id
    WHERE control.job_id=NEW.job_id
      AND control.ingress_assessment_revision_at_send=NEW.assessment_revision
      AND control.send_started_at=assessment.last_assessed_at
      AND assessment.assessment_revision=NEW.assessment_revision
      AND assessment.policy_version IS NEW.policy_version
      AND assessment.episode_generation IS NEW.episode_generation
      AND assessment.reason IS NEW.reason AND assessment.since IS NEW.since
      AND assessment.deadline IS NEW.deadline AND assessment.recovery_after IS NEW.recovery_after
      AND assessment.recovery_cutoff_epoch IS NEW.recovery_cutoff_epoch
      AND assessment.recovery_cutoff_sequence IS NEW.recovery_cutoff_sequence
      AND assessment.source_failure_epoch IS NEW.source_failure_epoch
      AND assessment.source_failure_sequence IS NEW.source_failure_sequence)
BEGIN SELECT RAISE(ABORT, 'ingress cause requires the exact first send assessment'); END;

CREATE TRIGGER codex_ingress_cause_immutable
BEFORE UPDATE ON codex_ingress_interrupt_causes
BEGIN SELECT RAISE(ABORT, 'ingress send cause is immutable'); END;
CREATE TRIGGER codex_ingress_cause_no_delete
BEFORE DELETE ON codex_ingress_interrupt_causes
BEGIN SELECT RAISE(ABORT, 'ingress send cause is retained'); END;

CREATE TRIGGER codex_ingress_capture_first_send
AFTER UPDATE ON codex_turn_controls
WHEN OLD.send_started_at IS NULL AND NEW.ingress_assessment_revision_at_send IS NOT NULL
BEGIN
    INSERT INTO codex_ingress_interrupt_causes
       (job_id,assessment_revision,policy_version,episode_generation,reason,since,deadline,
        recovery_after,recovery_cutoff_epoch,recovery_cutoff_sequence,
        source_failure_epoch,source_failure_sequence)
    SELECT job_id,assessment_revision,policy_version,episode_generation,reason,since,deadline,
           recovery_after,recovery_cutoff_epoch,recovery_cutoff_sequence,
           source_failure_epoch,source_failure_sequence
    FROM codex_telegram_ingress_assessments WHERE job_id=NEW.job_id;
END;
"""
