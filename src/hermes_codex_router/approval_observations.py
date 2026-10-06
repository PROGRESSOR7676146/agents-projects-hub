"""Shared, payload-free identity and copy for early and accepted approvals."""

from __future__ import annotations

import hashlib
import re
import sqlite3

from .codex_activity import CodexActivityEvent

IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", re.ASCII)
APPROVAL_CATEGORIES = {
    "command": "command execution",
    "file_change": "file changes",
    "network": "network access",
    "permissions": "additional permissions",
}


def identity_digest(value: str | int) -> str:
    return hashlib.sha256((type(value).__name__ + ":" + str(value)).encode()).hexdigest()


def approval_metadata(event: CodexActivityEvent) -> tuple[str, str]:
    if not isinstance(event, CodexActivityEvent):
        raise ValueError("invalid approval observation metadata")
    request = event.request_id
    if not (
        event.kind in {"approval_requested", "approval_resolved"}
        and event.category in APPROVAL_CATEGORIES
        and isinstance(event.item_id, str)
        and IDENTITY.fullmatch(event.item_id)
        and isinstance(event.turn_id, str)
        and IDENTITY.fullmatch(event.turn_id)
        and (
            (
                isinstance(request, int)
                and not isinstance(request, bool)
                and -(2**63) <= request < 2**63
            )
            or (isinstance(request, str) and IDENTITY.fullmatch(request))
        )
    ):
        raise ValueError("invalid approval observation metadata")
    assert isinstance(event.item_id, str) and isinstance(request, (str, int))
    return identity_digest(request), identity_digest(event.item_id)


def approval_event_key(job_id: str, identity: str) -> str:
    return f"activity:{job_id}:approval:{identity}"


def approval_notice_html(category: str) -> str:
    return (
        "A request for human permission for "
        + APPROVAL_CATEGORIES[category]
        + " was observed in this Codex session. Review the exact request in Codex/tlive "
        "to allow or deny it. Hub cannot approve it."
    )


def activity_metadata_count(connection: sqlite3.Connection, job_id: str) -> int:
    count = connection.execute(
        "SELECT count(*) FROM task_activity_entries WHERE job_id=?", (job_id,)
    ).fetchone()[0]
    if (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preacceptance_requests'"
        ).fetchone()
        is not None
    ):
        count += connection.execute(
            "SELECT count(*) FROM preacceptance_requests requests JOIN preacceptance_scopes scopes "
            "ON scopes.scope_id=requests.scope_id WHERE scopes.job_id=?",
            (job_id,),
        ).fetchone()[0]
    return count
