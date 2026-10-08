"""Dependency-neutral SQL for delivery FIFO only; never a general idle predicate."""

from .delivery_control_predicates import result_ready_control_reconciled


def outbox_delivery_hold_released(outbox: str) -> str:
    """Trusted internal SQL alias; match the immutable target and saved result."""
    return f"""({outbox}.status='unknown' AND EXISTS (
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
    ))"""


def job_blocks_topic_fifo(job: str) -> str:
    """Preserve nonterminal FIFO except an exact released saved-result delivery."""
    return f"""({job}.status NOT IN ('completed','failed','cancelled','indeterminate')
        AND NOT ({result_ready_control_reconciled(job)} OR ({job}.status='result_ready' AND EXISTS (
            SELECT 1 FROM telegram_outbox released_outbox
            JOIN provider_job_results released_result ON released_result.job_id={job}.job_id
            WHERE released_outbox.job_id={job}.job_id
              AND {outbox_delivery_hold_released("released_outbox")}
        ))))"""
