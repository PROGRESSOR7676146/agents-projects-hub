"""Dependency-neutral SQL for delivery FIFO only; never a general idle predicate."""

from .delivery_control_predicates import result_ready_control_reconciled


def nonblocking_execution_notice(outbox: str) -> str:
    """New explicitly classified notices only; legacy and real results stay conservative."""
    return f"""({outbox}.status='unknown' AND {outbox}.purpose='execution_notice'
        AND NOT EXISTS (SELECT 1 FROM provider_job_results r WHERE r.job_id={outbox}.job_id)
        AND EXISTS (SELECT 1 FROM provider_jobs j WHERE j.job_id={outbox}.job_id
                    AND j.status IN ('failed','cancelled','indeterminate')))"""


def outbox_delivery_hold_released(outbox: str) -> str:
    """Trusted internal SQL alias; match an exact immutable local or Hub record."""
    return f"""({outbox}.status='unknown' AND (
      EXISTS (
        SELECT 1 FROM telegram_delivery_hold_dispositions hold
        JOIN provider_jobs hold_job ON hold_job.job_id=hold.job_id
        JOIN topics hold_topic ON hold_topic.topic_id=hold_job.topic_id
        WHERE hold.outbox_id={outbox}.outbox_id AND hold.job_id={outbox}.job_id
          AND hold.sender_agent_id={outbox}.sender_agent_id
          AND hold.chat_id={outbox}.chat_id AND hold.thread_id={outbox}.thread_id
          AND hold_job.agent_id=hold.sender_agent_id AND hold_job.chat_id=hold.chat_id
          AND hold_job.topic_id=hold.topic_id AND hold_job.topic_sequence=hold.topic_sequence
          AND hold_job.session_id=hold.session_id
          AND hold_job.session_generation=hold.session_generation
          AND hold_topic.chat_id=hold.chat_id AND hold_topic.thread_id=hold.thread_id
          AND hold_topic.project_id=hold.project_id
          AND COALESCE(hold_topic.execution_scope,'project:' || hold_topic.project_id)=hold.execution_scope
          AND hold.result_id IS (SELECT result_id FROM provider_job_results
                                 WHERE job_id=hold.job_id)
    ) OR EXISTS (
        SELECT 1 FROM telegram_delivery_hold_remote_dispositions hold
        JOIN telegram_delivery_hold_callback_tokens token ON token.token_id=hold.token_id
        JOIN provider_jobs hold_job ON hold_job.job_id=hold.job_id
        JOIN topics hold_topic ON hold_topic.topic_id=hold_job.topic_id
        WHERE hold.outbox_id={outbox}.outbox_id AND hold.job_id={outbox}.job_id
          AND hold.sender_agent_id={outbox}.sender_agent_id
          AND hold.chat_id={outbox}.chat_id AND hold.thread_id={outbox}.thread_id
          AND hold_job.agent_id=hold.sender_agent_id AND hold_job.chat_id=hold.chat_id
          AND hold_job.topic_id=hold.topic_id AND hold_job.topic_sequence=hold.topic_sequence
          AND hold_job.session_id=hold.session_id
          AND hold_job.session_generation=hold.session_generation
          AND hold_topic.chat_id=hold.chat_id AND hold_topic.thread_id=hold.thread_id
          AND hold_topic.project_id=hold.project_id
          AND COALESCE(hold_topic.execution_scope,'project:' || hold_topic.project_id)=hold.execution_scope
          AND hold.result_id IS (SELECT result_id FROM provider_job_results
                                 WHERE job_id=hold.job_id)
          AND token.outbox_id=hold.outbox_id AND token.job_id=hold.job_id
          AND token.result_id IS hold.result_id AND token.topic_id=hold.topic_id
          AND token.topic_sequence=hold.topic_sequence AND token.project_id=hold.project_id
          AND token.execution_scope=hold.execution_scope AND token.session_id=hold.session_id
          AND token.session_generation=hold.session_generation
          AND token.sender_agent_id=hold.sender_agent_id AND token.chat_id=hold.chat_id
          AND token.thread_id=hold.thread_id AND token.owner_user_id=hold.owner_user_id
          AND token.hub_bot_user_id=hold.hub_bot_user_id
          AND token.notice_id=hold.notice_id
          AND token.notice_message_id=hold.notice_message_id
          AND token.preview_message_id=hold.callback_message_id
          AND token.callback_origin=hold.callback_origin
          AND token.authority=hold.authority AND token.action=hold.action
          AND token.snapshot_version=hold.snapshot_version AND token.snapshot=hold.snapshot
          AND hold.callback_origin='hub' AND hold.authority='telegram_owner_callback'
          AND hold.action='continue_without_confirmed_delivery'
          AND hold.snapshot_version=1
    )))"""


def remote_hold_has_full_control(hold: str) -> str:
    """A remote queue-only disposition keeps binding unless local full control matches it."""
    return f"""EXISTS (
        SELECT 1 FROM telegram_delivery_hold_remote_full_controls control
        WHERE control.disposition_id={hold}.disposition_id
    )"""


def job_blocks_topic_fifo(job: str) -> str:
    """Preserve nonterminal FIFO except an exact released saved-result delivery."""
    return f"""({job}.status NOT IN ('completed','failed','cancelled','indeterminate')
        AND NOT ({result_ready_control_reconciled(job)} OR ({job}.status='result_ready' AND EXISTS (
            SELECT 1 FROM telegram_outbox released_outbox
            JOIN provider_job_results released_result ON released_result.job_id={job}.job_id
            WHERE released_outbox.job_id={job}.job_id
              AND {outbox_delivery_hold_released("released_outbox")}
        ))))"""
