"""Assessment SQL on the caller-owned immediate transaction; never execution control."""

from __future__ import annotations

import html
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from .assessment_inputs import OutcomeAssessmentInput
from .task_lifecycle import TaskLifecycleState


@dataclass(frozen=True, slots=True)
class OutcomeAssessmentDisposition:
    disposition_id: str
    project_id: str
    topic_id: int | None
    chat_id: int
    thread_id: int
    input_message_id: int
    owner_user_id: int
    reply_message_id: int | None
    fingerprint_version: int
    input_fingerprint: str
    disposition: str
    refusal_code: str | None
    job_id: str | None
    result_id: str | None
    outbox_id: str | None
    decision: str | None
    reason: str | None
    predecessor_id: str | None
    revision: int | None
    created_at: str


def _eligible(connection: sqlite3.Connection, target: sqlite3.Row) -> bool:
    if target["delivery_status"] != "delivered":
        return False
    receipts = connection.execute(
        """SELECT COUNT(*),SUM(CASE WHEN telegram_message_id>0
             AND receipt_validation_version=1 THEN 1 ELSE 0 END)
           FROM telegram_outbox_parts WHERE outbox_id=?""",
        (target["outbox_id"],),
    ).fetchone()
    return receipts is not None and receipts[0] > 0 and receipts[0] == receipts[1]


def record_assessment(
    connection: sqlite3.Connection,
    notices: TaskLifecycleState,
    request: OutcomeAssessmentInput,
    *,
    project_id: str,
    owner_user_ids: Sequence[int],
    now: datetime,
    state_error: type[Exception],
) -> tuple[OutcomeAssessmentDisposition, bool]:
    if not connection.in_transaction:
        raise state_error("assessment_requires_transaction")
    if (
        type(request.owner_user_id) is not int
        or request.owner_user_id not in owner_user_ids
        or request.owner_user_id <= 0
    ):
        raise state_error("assessment_owner_required")
    if (
        type(request.chat_id) is not int
        or request.chat_id == 0
        or any(
            type(value) is not int or value <= 0
            for value in (request.thread_id, request.message_id)
        )
        or (
            request.reply_message_id is not None
            and (type(request.reply_message_id) is not int or request.reply_message_id <= 0)
        )
        or not isinstance(project_id, str)
        or not 1 <= len(project_id) <= 128
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise state_error("assessment_input_invalid")
    fingerprint = request.fingerprint()
    previous = connection.execute(
        "SELECT * FROM outcome_assessment_dispositions WHERE chat_id=? AND input_message_id=?",
        (request.chat_id, request.message_id),
    ).fetchone()
    if previous is not None:
        if previous["input_fingerprint"] != fingerprint or previous["project_id"] != project_id:
            raise state_error("assessment_input_conflict")
        return OutcomeAssessmentDisposition(**dict(previous)), False
    if (
        connection.execute(
            "SELECT 1 FROM observed_messages WHERE chat_id=? AND message_id=?",
            (request.chat_id, request.message_id),
        ).fetchone()
        is not None
    ):
        raise state_error("assessment_input_already_disposed")
    topic = connection.execute(
        "SELECT topic_id FROM topics WHERE project_id=? AND chat_id=? AND thread_id=?",
        (project_id, request.chat_id, request.thread_id),
    ).fetchone()
    refusal: str | None = None
    target: sqlite3.Row | None = None
    predecessor: sqlite3.Row | None = None
    parsed = request.parsed()
    if (
        request.is_forwarded
        or request.quote_text is not None
        or request.text_source != "text"
        or request.has_material
    ):
        refusal = "assessment_plain_owner_command_required"
    elif parsed is None:
        refusal = "assessment_syntax_invalid"
    elif topic is None or request.reply_message_id is None:
        refusal = "assessment_exact_result_reply_required"
    else:
        predecessor = connection.execute(
            """SELECT * FROM outcome_assessment_dispositions
               WHERE chat_id=? AND input_message_id=? AND project_id=? AND thread_id=?
                 AND topic_id=? AND disposition='applied'""",
            (request.chat_id, request.reply_message_id, project_id, request.thread_id, topic[0]),
        ).fetchone()
        if predecessor is not None:
            targets = connection.execute(
                """SELECT j.job_id,r.result_id,o.outbox_id,o.status AS delivery_status
                   FROM provider_jobs j JOIN provider_job_results r ON r.job_id=j.job_id
                   JOIN telegram_outbox o ON o.job_id=j.job_id
                   WHERE j.job_id=? AND r.result_id=? AND o.outbox_id=?
                     AND j.topic_id=? AND j.chat_id=? AND o.chat_id=? AND o.thread_id=?
                     AND o.sender_agent_id=j.agent_id""",
                (
                    predecessor["job_id"],
                    predecessor["result_id"],
                    predecessor["outbox_id"],
                    topic[0],
                    request.chat_id,
                    request.chat_id,
                    request.thread_id,
                ),
            ).fetchall()
        else:
            targets = connection.execute(
                """SELECT DISTINCT j.job_id,r.result_id,o.outbox_id,o.status AS delivery_status
                   FROM telegram_outbox_parts p JOIN telegram_outbox o ON o.outbox_id=p.outbox_id
                   JOIN provider_jobs j ON j.job_id=o.job_id
                   JOIN provider_job_results r ON r.job_id=j.job_id
                   WHERE p.telegram_message_id=? AND o.chat_id=? AND o.thread_id=?
                     AND j.chat_id=? AND j.topic_id=? AND o.sender_agent_id=j.agent_id LIMIT 2""",
                (
                    request.reply_message_id,
                    request.chat_id,
                    request.thread_id,
                    request.chat_id,
                    topic[0],
                ),
            ).fetchall()
        if len(targets) != 1:
            refusal = "assessment_result_unavailable"
        else:
            target = targets[0]
            assert target is not None
            if not _eligible(connection, target):
                refusal = "assessment_whole_result_receipts_required"
            else:
                head = connection.execute(
                    """SELECT disposition_id,revision FROM outcome_assessment_dispositions
                       WHERE result_id=? AND disposition='applied' ORDER BY revision DESC LIMIT 1""",
                    (target["result_id"],),
                ).fetchone()
                if (predecessor is None and head is not None) or (
                    predecessor is not None
                    and (head is None or head["disposition_id"] != predecessor["disposition_id"])
                ):
                    refusal = "assessment_latest_owner_command_reply_required"
    applied = refusal is None
    assert not applied or (parsed is not None and target is not None)
    disposition_id = uuid.uuid4().hex
    timestamp = now.astimezone(timezone.utc).isoformat()
    values = (
        disposition_id,
        project_id,
        topic[0] if topic is not None else None,
        request.chat_id,
        request.thread_id,
        request.message_id,
        request.owner_user_id,
        request.reply_message_id,
        1,
        fingerprint,
        "applied" if applied else "refused",
        refusal,
        target["job_id"] if target is not None else None,
        target["result_id"] if target is not None else None,
        target["outbox_id"] if target is not None else None,
        parsed[0] if applied and parsed is not None else None,
        parsed[1] if applied and parsed is not None else None,
        predecessor["disposition_id"] if applied and predecessor is not None else None,
        int(predecessor["revision"]) + 1
        if applied and predecessor is not None
        else 1
        if applied
        else None,
        timestamp,
    )
    connection.execute(
        """INSERT INTO outcome_assessment_dispositions
           (disposition_id,project_id,topic_id,chat_id,thread_id,input_message_id,owner_user_id,
            reply_message_id,fingerprint_version,input_fingerprint,disposition,refusal_code,
            job_id,result_id,outbox_id,decision,reason,predecessor_id,revision,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        values,
    )
    connection.execute(
        "INSERT INTO observed_messages VALUES (?,?,?,?)",
        (request.chat_id, request.message_id, "hub", timestamp),
    )
    copy = (
        f"Outcome recorded: {parsed[0]}. Reason: {html.escape(parsed[1])}"
        if applied and parsed is not None
        else "Outcome was not recorded. Reply to the whole delivered saved final with "
        "/assess accepted|rework|unknown REASON. To correct a decision, Reply to the latest applied owner /assess command."
    )
    notices.prepare_notice_in_transaction(
        event_key="outcome-assessment:" + disposition_id,
        kind="outcome_assessed",
        assessment_disposition_id=disposition_id,
        chat_id=request.chat_id,
        thread_id=request.thread_id,
        reply_to_message_id=request.message_id,
        telegram_html=copy,
        now=now,
    )
    row = connection.execute(
        "SELECT * FROM outcome_assessment_dispositions WHERE disposition_id=?", (disposition_id,)
    ).fetchone()
    assert row is not None
    return OutcomeAssessmentDisposition(**dict(row)), True
