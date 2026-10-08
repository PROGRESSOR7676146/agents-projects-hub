"""Schema 47: frozen exact full-consent exception to the schema44 binding guard.

This literal is deliberately independent of future runtime predicate changes.
"""

DELIVERY_CONTROL_ACTIVATION_SCHEMA = """
DROP TRIGGER telegram_delivery_hold_topic_binding_guard;
CREATE TRIGGER telegram_delivery_hold_topic_binding_guard
BEFORE UPDATE OF project_id,execution_scope,chat_id,thread_id ON topics
WHEN (NEW.project_id IS NOT OLD.project_id OR NEW.execution_scope IS NOT OLD.execution_scope
      OR NEW.chat_id IS NOT OLD.chat_id OR NEW.thread_id IS NOT OLD.thread_id)
 AND EXISTS (
   SELECT 1 FROM telegram_delivery_hold_dispositions hold WHERE hold.topic_id=OLD.topic_id
     AND (NEW.project_id IS NOT hold.project_id OR NEW.chat_id IS NOT hold.chat_id
          OR NEW.thread_id IS NOT hold.thread_id OR NOT EXISTS (
        SELECT 1 FROM telegram_outbox control_held_final
        JOIN telegram_delivery_control_dispositions control_consent
          ON control_consent.outbox_id=control_held_final.outbox_id
        WHERE control_held_final.outbox_id=hold.outbox_id
          AND ((control_held_final.status='unknown' OR
                  (control_held_final.status='failed' AND control_held_final.attempt_count=20))
        AND control_held_final.lease_owner IS NULL AND control_held_final.lease_token IS NULL
        AND control_held_final.lease_expires_at IS NULL AND EXISTS (
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
                       AND (checkpoint.project_root='/'
                            OR substr(checkpoint.project_root,-1)!='/')))
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
          AND control_consent.thread_id=hold.thread_id
    ))
 )
BEGIN SELECT RAISE(ABORT, 'delivery hold disposition retains its topic binding'); END;
"""
