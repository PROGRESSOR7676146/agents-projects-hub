"""Schema51: retained causal polls and exact-target assessments, without control."""

INGRESS_ASSESSMENT_SCHEMA = """
CREATE TRIGGER telegram_group_ingress_no_replace
BEFORE INSERT ON telegram_group_ingress
WHEN EXISTS(SELECT 1 FROM telegram_group_ingress
    WHERE identity=NEW.identity OR rowid=NEW.rowid)
BEGIN SELECT RAISE(ABORT, 'group ingress cannot be replaced'); END;
CREATE TRIGGER telegram_group_ingress_rowid_immutable
BEFORE UPDATE ON telegram_group_ingress WHEN NEW.rowid IS NOT OLD.rowid
BEGIN SELECT RAISE(ABORT, 'group ingress row identity is immutable'); END;

CREATE TABLE telegram_ingress_watermarks (
    identity TEXT PRIMARY KEY NOT NULL REFERENCES telegram_group_ingress(identity)
        CHECK(identity IN ('hub','codex')),
    success_epoch INTEGER,
    success_sequence INTEGER,
    success_at TEXT,
    failure_witness_epoch INTEGER,
    failure_witness_sequence INTEGER,
    failure_threshold_at TEXT,
    CHECK((success_epoch IS NULL AND success_sequence IS NULL AND success_at IS NULL)
       OR (success_epoch IS NOT NULL AND success_sequence IS NOT NULL
           AND success_epoch BETWEEN 1 AND 9223372036854775807
           AND success_sequence BETWEEN 1 AND 9223372036854775807
           AND success_at IS NOT NULL)),
    CHECK((failure_witness_epoch IS NULL AND failure_witness_sequence IS NULL
           AND failure_threshold_at IS NULL)
       OR (failure_witness_epoch IS NOT NULL AND failure_witness_sequence IS NOT NULL
           AND failure_witness_epoch BETWEEN 1 AND 9223372036854775807
           AND failure_witness_sequence BETWEEN 1 AND 9223372036854775807
           AND failure_threshold_at IS NOT NULL)),
    CHECK(success_epoch IS NULL OR (typeof(success_epoch)='integer' AND typeof(success_sequence)='integer')),
    CHECK(failure_witness_epoch IS NULL OR (typeof(failure_witness_epoch)='integer' AND typeof(failure_witness_sequence)='integer')),
    CHECK(failure_witness_epoch IS NULL OR success_epoch IS NULL
       OR (failure_witness_epoch,failure_witness_sequence)>(success_epoch,success_sequence))
);
CREATE TRIGGER telegram_ingress_watermark_no_replace
BEFORE INSERT ON telegram_ingress_watermarks
WHEN EXISTS(SELECT 1 FROM telegram_ingress_watermarks
    WHERE identity=NEW.identity OR rowid=NEW.rowid)
BEGIN SELECT RAISE(ABORT, 'ingress watermark cannot be replaced'); END;
CREATE TRIGGER telegram_ingress_watermark_fence
BEFORE UPDATE ON telegram_ingress_watermarks
WHEN NEW.identity IS NOT OLD.identity OR NEW.rowid IS NOT OLD.rowid
 OR (OLD.success_epoch IS NOT NULL AND
     (NEW.success_epoch IS NULL OR
      (NEW.success_epoch,NEW.success_sequence)<(OLD.success_epoch,OLD.success_sequence)
      OR NEW.success_at<OLD.success_at
      OR ((NEW.success_epoch,NEW.success_sequence)=(OLD.success_epoch,OLD.success_sequence)
          AND NEW.success_at IS NOT OLD.success_at)))
 OR (OLD.failure_witness_epoch IS NOT NULL AND NOT (
      (NEW.failure_witness_epoch IS OLD.failure_witness_epoch
       AND NEW.failure_witness_sequence IS OLD.failure_witness_sequence
       AND NEW.failure_threshold_at IS OLD.failure_threshold_at)
      OR (NEW.failure_witness_epoch IS NULL AND NEW.success_epoch IS NOT NULL
          AND (NEW.success_epoch,NEW.success_sequence)>
              (OLD.failure_witness_epoch,OLD.failure_witness_sequence)
          AND NEW.success_at>=OLD.failure_threshold_at)))
BEGIN SELECT RAISE(ABORT, 'ingress watermark cannot regress'); END;
CREATE TRIGGER telegram_ingress_watermark_no_delete
BEFORE DELETE ON telegram_ingress_watermarks
BEGIN SELECT RAISE(ABORT, 'ingress watermark is retained'); END;

CREATE TABLE codex_telegram_ingress_assessments (
    job_id TEXT PRIMARY KEY NOT NULL REFERENCES codex_telegram_precaution_targets(job_id),
    policy_version INTEGER NOT NULL CHECK(policy_version=1),
    assessment_revision INTEGER NOT NULL CHECK(assessment_revision BETWEEN 1 AND 9223372036854775807),
    last_assessed_at TEXT NOT NULL,
    last_read_epoch INTEGER,
    last_read_sequence INTEGER,
    last_confirmed_poll_at TEXT,
    recent_poll_confirmed INTEGER NOT NULL CHECK(recent_poll_confirmed IN (0,1)),
    episode_generation INTEGER NOT NULL CHECK(episode_generation BETWEEN 0 AND 9223372036854775807),
    reason TEXT CHECK(reason IN ('never_confirmed','stale_or_missing','poll_failures')),
    since TEXT,
    deadline TEXT,
    recovery_after TEXT,
    recovery_cutoff_epoch INTEGER,
    recovery_cutoff_sequence INTEGER,
    source_failure_epoch INTEGER,
    source_failure_sequence INTEGER,
    CHECK((last_read_epoch IS NULL AND last_read_sequence IS NULL)
       OR (last_read_epoch IS NOT NULL AND last_read_sequence IS NOT NULL
           AND last_read_epoch BETWEEN 1 AND 9223372036854775807
           AND last_read_sequence BETWEEN 0 AND 9223372036854775807)),
    CHECK((recovery_cutoff_epoch IS NULL AND recovery_cutoff_sequence IS NULL)
       OR (recovery_cutoff_epoch IS NOT NULL AND recovery_cutoff_sequence IS NOT NULL
           AND recovery_cutoff_epoch BETWEEN 1 AND 9223372036854775807
           AND recovery_cutoff_sequence BETWEEN 0 AND 9223372036854775807)),
    CHECK((source_failure_epoch IS NULL AND source_failure_sequence IS NULL)
       OR (source_failure_epoch IS NOT NULL AND source_failure_sequence IS NOT NULL
           AND source_failure_epoch BETWEEN 1 AND 9223372036854775807
           AND source_failure_sequence BETWEEN 1 AND 9223372036854775807)),
    CHECK((reason IS NULL AND since IS NULL AND deadline IS NULL AND recovery_after IS NULL
           AND recovery_cutoff_epoch IS NULL AND source_failure_epoch IS NULL)
       OR (reason IS NOT NULL AND since IS NOT NULL AND deadline IS NOT NULL
           AND recovery_after IS NOT NULL AND deadline>=since AND episode_generation>0
           AND recent_poll_confirmed=0)),
    CHECK(source_failure_epoch IS NULL OR (reason='poll_failures'
       AND recovery_cutoff_epoch IS NOT NULL AND recovery_cutoff_sequence IS NOT NULL
       AND source_failure_epoch=recovery_cutoff_epoch
       AND source_failure_sequence=recovery_cutoff_sequence)),
    CHECK(recovery_cutoff_epoch IS NULL OR (last_read_epoch IS NOT NULL
       AND (recovery_cutoff_epoch,recovery_cutoff_sequence)<=(last_read_epoch,last_read_sequence))),
    CHECK(recent_poll_confirmed=0 OR last_confirmed_poll_at IS NOT NULL),
    CHECK(typeof(assessment_revision)='integer' AND typeof(episode_generation)='integer'),
    CHECK(last_read_epoch IS NULL OR (typeof(last_read_epoch)='integer' AND typeof(last_read_sequence)='integer')),
    CHECK(recovery_cutoff_epoch IS NULL OR (typeof(recovery_cutoff_epoch)='integer' AND typeof(recovery_cutoff_sequence)='integer')),
    CHECK(source_failure_epoch IS NULL OR (typeof(source_failure_epoch)='integer' AND typeof(source_failure_sequence)='integer'))
);
CREATE TRIGGER codex_ingress_assessment_no_replace
BEFORE INSERT ON codex_telegram_ingress_assessments
WHEN EXISTS(SELECT 1 FROM codex_telegram_ingress_assessments
    WHERE job_id=NEW.job_id OR rowid=NEW.rowid)
BEGIN SELECT RAISE(ABORT, 'ingress assessment cannot be replaced'); END;
CREATE TRIGGER codex_ingress_assessment_fence
BEFORE UPDATE ON codex_telegram_ingress_assessments
WHEN NEW.job_id IS NOT OLD.job_id OR NEW.rowid IS NOT OLD.rowid
 OR NEW.policy_version IS NOT OLD.policy_version
 OR NEW.assessment_revision!=OLD.assessment_revision+1
 OR NEW.last_assessed_at<OLD.last_assessed_at
 OR NEW.episode_generation<OLD.episode_generation
 OR NEW.episode_generation>OLD.episode_generation+1
 OR (OLD.last_read_epoch IS NOT NULL AND (NEW.last_read_epoch IS NULL
     OR (NEW.last_read_epoch,NEW.last_read_sequence)<(OLD.last_read_epoch,OLD.last_read_sequence)))
 OR (OLD.last_confirmed_poll_at IS NOT NULL AND (NEW.last_confirmed_poll_at IS NULL
     OR NEW.last_confirmed_poll_at<OLD.last_confirmed_poll_at))
 OR (OLD.reason IS NULL AND NEW.reason IS NOT NULL
     AND NEW.episode_generation!=OLD.episode_generation+1)
 OR (NEW.reason IS NULL AND NEW.episode_generation!=OLD.episode_generation)
 OR (OLD.reason IS NOT NULL AND NEW.reason IS NOT NULL
     AND NEW.episode_generation=OLD.episode_generation
     AND (NEW.deadline>OLD.deadline OR (NEW.deadline=OLD.deadline AND
       (NEW.reason IS NOT OLD.reason OR NEW.since IS NOT OLD.since
        OR NEW.recovery_after IS NOT OLD.recovery_after
        OR NEW.recovery_cutoff_epoch IS NOT OLD.recovery_cutoff_epoch
        OR NEW.recovery_cutoff_sequence IS NOT OLD.recovery_cutoff_sequence
        OR NEW.source_failure_epoch IS NOT OLD.source_failure_epoch
        OR NEW.source_failure_sequence IS NOT OLD.source_failure_sequence))))
BEGIN SELECT RAISE(ABORT, 'ingress assessment cannot regress'); END;
CREATE TRIGGER codex_ingress_assessment_no_delete
BEFORE DELETE ON codex_telegram_ingress_assessments
BEGIN SELECT RAISE(ABORT, 'ingress assessment is retained'); END;
"""
