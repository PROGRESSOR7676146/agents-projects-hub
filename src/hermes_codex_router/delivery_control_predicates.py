"""Exact historical delivery consent; no native, writer or general-idle authority.

SQL aliases are trusted internal constants, never user input.
"""


def _historical_binding(target: str, kind: str) -> str:
    key = "outbox_id" if kind == "final_outbox" else "progress_id"
    identity = (
        """dj.status IN ('result_ready','failed','cancelled','indeterminate')
          AND (dj.status!='result_ready' OR EXISTS (
              SELECT 1 FROM provider_job_results WHERE job_id=dj.job_id))
          AND dc.result_id IS (SELECT result_id FROM provider_job_results WHERE job_id=dj.job_id)"""
        if kind == "final_outbox"
        else f"""dc.result_id IS NULL AND dc.item_sequence={target}.item_sequence
          AND EXISTS (SELECT 1 FROM provider_visible_items item
                      WHERE item.sequence=dc.item_sequence AND item.job_id=dj.job_id
                        AND item.phase='commentary')"""
    )
    proof = ""
    if kind == "final_outbox":
        proof = f"""AND (NOT ({target}.status='failed' AND dj.status='indeterminate')
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
                            OR substr(checkpoint.project_root,-1)!='/')))"""
    return f"""EXISTS (
        SELECT 1 FROM telegram_delivery_control_dispositions dc
        JOIN provider_jobs dj ON dj.job_id=dc.job_id
        JOIN topics dt ON dt.topic_id=dj.topic_id
        JOIN agent_sessions ds ON ds.session_id=dj.session_id
        WHERE dc.target_kind='{kind}' AND dc.{key}={target}.{key}
          AND dc.action='reconcile_delivery_control_wait'
          AND dc.snapshot_version=1 AND dc.authority='local_owner_cli'
          AND dc.job_id={target}.job_id AND dc.sender_agent_id={target}.sender_agent_id
          AND dc.chat_id={target}.chat_id AND dc.thread_id={target}.thread_id
          AND dc.delivery_status_at_consent={target}.status
          AND dj.agent_id=dc.sender_agent_id AND dj.chat_id=dc.chat_id
          AND dj.topic_id=dc.topic_id AND dj.topic_sequence=dc.topic_sequence
          AND dj.session_id=dc.session_id AND dj.session_generation=dc.session_generation
          AND dt.project_id=dc.project_id AND dt.chat_id=dc.chat_id AND dt.thread_id=dc.thread_id
          AND ds.topic_id=dc.topic_id AND ds.agent_id=dc.sender_agent_id
          AND ds.generation=dc.session_generation AND {identity}
          {proof}
    )"""


def final_control_reconciled(outbox: str) -> str:
    return _parked(outbox, "final_outbox")


def progress_control_reconciled(progress: str) -> str:
    return _parked(progress, "progress_delivery")


def _parked(target: str, kind: str) -> str:
    return f"""(({target}.status='unknown' OR
                  ({target}.status='failed' AND {target}.attempt_count=20))
        AND {target}.lease_owner IS NULL AND {target}.lease_token IS NULL
        AND {target}.lease_expires_at IS NULL AND {_historical_binding(target, kind)})"""


def result_ready_control_reconciled(job: str) -> str:
    """Only an exact saved final can release a result-ready delivery clause."""
    return f"""({job}.status='result_ready' AND EXISTS (
        SELECT 1 FROM telegram_outbox control_final
        JOIN provider_job_results control_result ON control_result.job_id={job}.job_id
        WHERE control_final.job_id={job}.job_id
          AND {final_control_reconciled("control_final")}
    ))"""


def legacy_hold_has_full_control(hold: str) -> str:
    """Each old queue-only consent needs its own exact, independent full consent."""
    return f"""EXISTS (
        SELECT 1 FROM telegram_outbox control_held_final
        JOIN telegram_delivery_control_dispositions control_consent
          ON control_consent.outbox_id=control_held_final.outbox_id
        WHERE control_held_final.outbox_id={hold}.outbox_id
          AND {final_control_reconciled("control_held_final")}
          AND control_consent.job_id={hold}.job_id
          AND control_consent.result_id IS {hold}.result_id
          AND control_consent.topic_id={hold}.topic_id
          AND control_consent.topic_sequence={hold}.topic_sequence
          AND control_consent.project_id={hold}.project_id
          AND control_consent.session_id={hold}.session_id
          AND control_consent.session_generation={hold}.session_generation
          AND control_consent.sender_agent_id={hold}.sender_agent_id
          AND control_consent.chat_id={hold}.chat_id
          AND control_consent.thread_id={hold}.thread_id
    )"""
