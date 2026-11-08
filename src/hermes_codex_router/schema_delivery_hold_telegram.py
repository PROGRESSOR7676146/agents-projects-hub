"""Schema 56: Hub-Bot owner recovery of one unknown delivery hold.

The established local-owner ledger remains untouched.  Remote consent has a
separate immutable provenance ledger so a Telegram callback can never be
misdescribed as local OS authority.
"""

DELIVERY_HOLD_TELEGRAM_SCHEMA = """
CREATE TABLE telegram_delivery_hold_callback_tokens (
    token_id TEXT PRIMARY KEY CHECK(length(token_id) BETWEEN 1 AND 128),
    token_sha256 TEXT NOT NULL UNIQUE
        CHECK(length(token_sha256)=64 AND token_sha256 NOT GLOB '*[^0-9a-f]*'),
    outbox_id TEXT NOT NULL REFERENCES telegram_outbox(outbox_id),
    job_id TEXT NOT NULL REFERENCES provider_jobs(job_id),
    result_id TEXT REFERENCES provider_job_results(result_id),
    topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    topic_sequence INTEGER NOT NULL CHECK(topic_sequence > 0),
    project_id TEXT NOT NULL CHECK(length(project_id) BETWEEN 1 AND 128),
    execution_scope TEXT NOT NULL
        CHECK(length(execution_scope) BETWEEN 6 AND 4101
              AND substr(execution_scope,1,6)='root:/'),
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    session_generation INTEGER NOT NULL CHECK(session_generation > 0),
    sender_agent_id TEXT NOT NULL CHECK(length(sender_agent_id) BETWEEN 1 AND 64),
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    thread_id INTEGER NOT NULL CHECK(thread_id > 0),
    owner_user_id INTEGER NOT NULL CHECK(owner_user_id > 0),
    hub_bot_user_id INTEGER NOT NULL CHECK(hub_bot_user_id > 0),
    notice_id TEXT NOT NULL REFERENCES task_lifecycle_notices(notice_id),
    notice_message_id INTEGER NOT NULL CHECK(notice_message_id > 0),
    preview_message_id INTEGER NOT NULL CHECK(preview_message_id > 0),
    callback_origin TEXT NOT NULL CHECK(callback_origin='hub'),
    authority TEXT NOT NULL CHECK(authority='telegram_owner_callback'),
    action TEXT NOT NULL CHECK(action='continue_without_confirmed_delivery'),
    snapshot_version INTEGER NOT NULL CHECK(snapshot_version=1),
    snapshot TEXT NOT NULL CHECK(length(snapshot)=64 AND snapshot NOT GLOB '*[^0-9a-f]*'),
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL CHECK(expires_at > issued_at)
);
CREATE INDEX telegram_delivery_hold_callback_token_lookup
ON telegram_delivery_hold_callback_tokens(token_sha256, expires_at);
CREATE INDEX telegram_delivery_hold_callback_token_outbox
ON telegram_delivery_hold_callback_tokens(outbox_id, owner_user_id, expires_at);
CREATE TRIGGER telegram_delivery_hold_callback_tokens_no_update
BEFORE UPDATE ON telegram_delivery_hold_callback_tokens
BEGIN SELECT RAISE(ABORT, 'delivery hold callback tokens are immutable'); END;
CREATE TRIGGER telegram_delivery_hold_callback_tokens_no_delete
BEFORE DELETE ON telegram_delivery_hold_callback_tokens
BEGIN SELECT RAISE(ABORT, 'delivery hold callback tokens cannot be deleted'); END;

CREATE TABLE telegram_delivery_hold_remote_dispositions (
    disposition_id TEXT PRIMARY KEY CHECK(length(disposition_id) BETWEEN 1 AND 128),
    token_id TEXT NOT NULL UNIQUE REFERENCES telegram_delivery_hold_callback_tokens(token_id),
    callback_id TEXT NOT NULL UNIQUE CHECK(length(callback_id) BETWEEN 1 AND 200),
    outbox_id TEXT NOT NULL UNIQUE REFERENCES telegram_outbox(outbox_id),
    job_id TEXT NOT NULL UNIQUE REFERENCES provider_jobs(job_id),
    result_id TEXT REFERENCES provider_job_results(result_id),
    topic_id INTEGER NOT NULL REFERENCES topics(topic_id),
    topic_sequence INTEGER NOT NULL CHECK(topic_sequence > 0),
    project_id TEXT NOT NULL CHECK(length(project_id) BETWEEN 1 AND 128),
    execution_scope TEXT NOT NULL
        CHECK(length(execution_scope) BETWEEN 6 AND 4101
              AND substr(execution_scope,1,6)='root:/'),
    session_id TEXT NOT NULL REFERENCES agent_sessions(session_id),
    session_generation INTEGER NOT NULL CHECK(session_generation > 0),
    sender_agent_id TEXT NOT NULL CHECK(length(sender_agent_id) BETWEEN 1 AND 64),
    chat_id INTEGER NOT NULL CHECK(chat_id != 0),
    thread_id INTEGER NOT NULL CHECK(thread_id > 0),
    owner_user_id INTEGER NOT NULL CHECK(owner_user_id > 0),
    hub_bot_user_id INTEGER NOT NULL CHECK(hub_bot_user_id > 0),
    notice_id TEXT NOT NULL REFERENCES task_lifecycle_notices(notice_id),
    notice_message_id INTEGER NOT NULL CHECK(notice_message_id > 0),
    callback_message_id INTEGER NOT NULL CHECK(callback_message_id > 0),
    callback_origin TEXT NOT NULL CHECK(callback_origin='hub'),
    authority TEXT NOT NULL CHECK(authority='telegram_owner_callback'),
    action TEXT NOT NULL CHECK(action='continue_without_confirmed_delivery'),
    snapshot_version INTEGER NOT NULL CHECK(snapshot_version=1),
    snapshot TEXT NOT NULL CHECK(length(snapshot)=64 AND snapshot NOT GLOB '*[^0-9a-f]*'),
    applied_at TEXT NOT NULL
);
CREATE TRIGGER telegram_delivery_hold_remote_dispositions_no_update
BEFORE UPDATE ON telegram_delivery_hold_remote_dispositions
BEGIN SELECT RAISE(ABORT, 'remote delivery hold dispositions are immutable'); END;
CREATE TRIGGER telegram_delivery_hold_remote_dispositions_no_delete
BEFORE DELETE ON telegram_delivery_hold_remote_dispositions
BEGIN SELECT RAISE(ABORT, 'remote delivery hold dispositions cannot be deleted'); END;

CREATE TRIGGER telegram_delivery_hold_remote_dispositions_no_legacy_conflict
BEFORE INSERT ON telegram_delivery_hold_remote_dispositions
WHEN EXISTS (
    SELECT 1 FROM telegram_delivery_hold_dispositions legacy
    WHERE legacy.outbox_id=NEW.outbox_id
)
BEGIN SELECT RAISE(ABORT, 'delivery hold already has a local disposition'); END;
CREATE TRIGGER telegram_delivery_hold_dispositions_no_remote_conflict
BEFORE INSERT ON telegram_delivery_hold_dispositions
WHEN EXISTS (
    SELECT 1 FROM telegram_delivery_hold_remote_dispositions remote
    WHERE remote.outbox_id=NEW.outbox_id
)
BEGIN SELECT RAISE(ABORT, 'delivery hold already has a remote disposition'); END;

-- This frozen view is the remote equivalent of schema47's full-control test.
-- It does not grant full control itself: it only lets the existing separate
-- local full-control disposition satisfy the retained topic-binding guard.
CREATE VIEW telegram_delivery_hold_remote_full_controls AS
SELECT hold.disposition_id
FROM telegram_delivery_hold_remote_dispositions hold
JOIN telegram_delivery_hold_callback_tokens token ON token.token_id=hold.token_id
JOIN telegram_outbox control_held_final
  ON control_held_final.outbox_id=hold.outbox_id
JOIN telegram_delivery_control_dispositions control_consent
  ON control_consent.outbox_id=control_held_final.outbox_id
WHERE token.outbox_id=hold.outbox_id
 AND token.job_id=hold.job_id
 AND token.result_id IS hold.result_id
 AND token.topic_id=hold.topic_id
 AND token.topic_sequence=hold.topic_sequence
 AND token.project_id=hold.project_id
 AND token.execution_scope=hold.execution_scope
 AND token.session_id=hold.session_id
 AND token.session_generation=hold.session_generation
 AND token.sender_agent_id=hold.sender_agent_id
 AND token.chat_id=hold.chat_id
 AND token.thread_id=hold.thread_id
 AND token.owner_user_id=hold.owner_user_id
 AND token.hub_bot_user_id=hold.hub_bot_user_id
 AND token.notice_id=hold.notice_id
 AND token.notice_message_id=hold.notice_message_id
 AND token.preview_message_id=hold.callback_message_id
 AND token.callback_origin=hold.callback_origin
 AND token.authority=hold.authority
 AND token.action=hold.action
 AND token.snapshot_version=hold.snapshot_version
 AND token.snapshot=hold.snapshot
 AND ((control_held_final.status='unknown' OR
        (control_held_final.status='failed' AND control_held_final.attempt_count=20))
       AND control_held_final.lease_owner IS NULL
       AND control_held_final.lease_token IS NULL
       AND control_held_final.lease_expires_at IS NULL
       AND EXISTS (
    SELECT 1 FROM telegram_delivery_control_dispositions dc
    JOIN provider_jobs dj ON dj.job_id=dc.job_id
    JOIN topics dt ON dt.topic_id=dj.topic_id
    JOIN agent_sessions ds ON ds.session_id=dj.session_id
    WHERE dc.target_kind='final_outbox' AND dc.outbox_id=control_held_final.outbox_id
      AND dc.action='reconcile_delivery_control_wait'
      AND dc.snapshot_version=1 AND dc.authority='local_owner_cli'
      AND dc.job_id=control_held_final.job_id AND dc.sender_agent_id=control_held_final.sender_agent_id
      AND dc.chat_id=control_held_final.chat_id AND dc.thread_id=control_held_final.thread_id
      AND dc.delivery_status_at_consent=control_held_final.status
      AND dj.agent_id=dc.sender_agent_id AND dj.chat_id=dc.chat_id
      AND dj.topic_id=dc.topic_id AND dj.topic_sequence=dc.topic_sequence
      AND dj.session_id=dc.session_id AND dj.session_generation=dc.session_generation
      AND dt.project_id=dc.project_id AND dt.chat_id=dc.chat_id AND dt.thread_id=dc.thread_id
      AND ds.topic_id=dc.topic_id AND ds.agent_id=dc.sender_agent_id
      AND ds.generation=dc.session_generation
      AND dj.status IN ('result_ready','failed','cancelled','indeterminate')
      AND (dj.status!='result_ready' OR EXISTS (
          SELECT 1 FROM provider_job_results WHERE job_id=dj.job_id))
      AND dc.result_id IS (SELECT result_id FROM provider_job_results WHERE job_id=dj.job_id)
      AND (NOT (control_held_final.status='failed' AND dj.status='indeterminate')
      OR EXISTS (SELECT 1 FROM provider_job_resolutions resolution
                 WHERE resolution.job_id=dc.resolution_job_id
                   AND resolution.job_id=dj.job_id
                   AND resolution.resolution IN
                       ('acknowledged','superseded','externally_completed'))
      OR EXISTS (SELECT 1 FROM provider_turn_terminal_evidence evidence
                 JOIN provider_execution_checkpoints checkpoint
                   ON checkpoint.job_id=evidence.job_id
                 WHERE evidence.job_id=dc.terminal_evidence_job_id
                   AND evidence.job_id=dj.job_id
                   AND evidence.terminal_status IN ('completed','failed','interrupted')
                   AND evidence.provider_thread_id=checkpoint.provider_thread_id
                   AND evidence.provider_turn_id=checkpoint.provider_turn_id
                   AND length(checkpoint.provider_thread_id) BETWEEN 1 AND 256
                   AND length(checkpoint.provider_turn_id) BETWEEN 1 AND 256
                   AND evidence.project_root=checkpoint.project_root
                   AND length(checkpoint.project_root) BETWEEN 1 AND 4096
                   AND substr(checkpoint.project_root,1,1)='/'
                   AND instr(checkpoint.project_root,char(0))=0
                   AND instr(checkpoint.project_root,'//')=0
                   AND instr(checkpoint.project_root,'/./')=0
                   AND instr(checkpoint.project_root,'/../')=0
                   AND checkpoint.project_root NOT LIKE '%/.'
                   AND checkpoint.project_root NOT LIKE '%/..'
                   AND (checkpoint.project_root='/' OR substr(checkpoint.project_root,-1)!='/')))
 ))
 AND control_consent.job_id=hold.job_id
 AND control_consent.result_id IS hold.result_id
 AND control_consent.topic_id=hold.topic_id
 AND control_consent.topic_sequence=hold.topic_sequence
 AND control_consent.project_id=hold.project_id
 AND control_consent.session_id=hold.session_id
 AND control_consent.session_generation=hold.session_generation
 AND control_consent.sender_agent_id=hold.sender_agent_id
 AND control_consent.chat_id=hold.chat_id
 AND control_consent.thread_id=hold.thread_id;

CREATE TRIGGER telegram_delivery_hold_remote_topic_binding_guard
BEFORE UPDATE OF project_id,execution_scope,chat_id,thread_id ON topics
WHEN (NEW.project_id IS NOT OLD.project_id OR NEW.execution_scope IS NOT OLD.execution_scope
      OR NEW.chat_id IS NOT OLD.chat_id OR NEW.thread_id IS NOT OLD.thread_id)
 AND EXISTS (
   SELECT 1 FROM telegram_delivery_hold_remote_dispositions hold
   WHERE hold.topic_id=OLD.topic_id
     AND (NEW.project_id IS NOT hold.project_id OR NEW.chat_id IS NOT hold.chat_id
          OR NEW.thread_id IS NOT hold.thread_id OR NOT EXISTS (
              SELECT 1 FROM telegram_delivery_hold_remote_full_controls full_control
              WHERE full_control.disposition_id=hold.disposition_id
          ))
 )
BEGIN SELECT RAISE(ABORT, 'remote delivery hold disposition retains its topic binding'); END;
"""
