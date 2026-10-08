"""Schema49: fenced actual group polls, without control or target authority."""

TELEGRAM_INGRESS_SCHEMA = """
CREATE TABLE telegram_group_ingress (
    identity TEXT PRIMARY KEY CHECK(identity IN ('hub','codex')),
    epoch INTEGER NOT NULL CHECK(epoch BETWEEN 1 AND 9223372036854775807),
    instance_token_hash TEXT NOT NULL CHECK(length(instance_token_hash)=64),
    registered_at TEXT NOT NULL,
    poll_sequence INTEGER NOT NULL CHECK(poll_sequence BETWEEN 0 AND 9223372036854775807),
    last_poll_at TEXT,
    last_poll_succeeded INTEGER CHECK(last_poll_succeeded IN (0,1)),
    last_success_at TEXT,
    last_confirmed_poll_at TEXT,
    failure_streak INTEGER NOT NULL CHECK(failure_streak BETWEEN 0 AND 1000000),
    failure_threshold_at TEXT,
    CHECK((poll_sequence=0 AND last_poll_at IS NULL AND last_poll_succeeded IS NULL
           AND last_success_at IS NULL AND failure_streak=0 AND failure_threshold_at IS NULL)
       OR (poll_sequence>0 AND last_poll_at IS NOT NULL AND last_poll_succeeded IS NOT NULL)),
    CHECK((failure_streak<3 AND failure_threshold_at IS NULL)
       OR (failure_streak>=3 AND failure_threshold_at IS NOT NULL)),
    CHECK(last_poll_succeeded IS NOT 1 OR (failure_streak=0 AND last_success_at=last_poll_at)),
    CHECK(last_poll_succeeded IS NOT 0 OR failure_streak>0),
    CHECK(last_success_at IS NULL OR (last_confirmed_poll_at IS NOT NULL
       AND last_success_at<=last_confirmed_poll_at AND last_success_at<=last_poll_at)),
    CHECK(last_poll_at IS NULL OR last_poll_at>=registered_at)
);
CREATE TRIGGER telegram_group_ingress_fence
BEFORE UPDATE ON telegram_group_ingress
WHEN NEW.identity IS NOT OLD.identity
 OR (OLD.last_confirmed_poll_at IS NOT NULL AND
     (NEW.last_confirmed_poll_at IS NULL OR NEW.last_confirmed_poll_at<OLD.last_confirmed_poll_at))
 OR NOT (
    (NEW.epoch=OLD.epoch AND NEW.instance_token_hash=OLD.instance_token_hash
     AND NEW.registered_at=OLD.registered_at AND NEW.poll_sequence>OLD.poll_sequence
     AND (OLD.last_poll_at IS NULL OR NEW.last_poll_at>=OLD.last_poll_at))
    OR (NEW.epoch=OLD.epoch+1 AND NEW.instance_token_hash!=OLD.instance_token_hash
        AND NEW.poll_sequence=0 AND NEW.registered_at>=COALESCE(OLD.last_poll_at,OLD.registered_at)
        AND NEW.last_confirmed_poll_at IS OLD.last_confirmed_poll_at)
 )
BEGIN SELECT RAISE(ABORT, 'group ingress evidence fence cannot reset'); END;
CREATE TRIGGER telegram_group_ingress_no_delete
BEFORE DELETE ON telegram_group_ingress
BEGIN SELECT RAISE(ABORT, 'group ingress epochs are retained'); END;
"""
