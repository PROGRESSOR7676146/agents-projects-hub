"""Input-bound refusals, independent of jobs and persistent root blockers."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from typing import Callable, Literal

from .state_errors import CodexPermissionSelectionChanged, StateError

PermissionInputDisposition = Literal["rejected", "duplicate"] | None


class CodexPermissionInputState:
    def __init__(
        self,
        connection: sqlite3.Connection,
        transaction: Callable[[], AbstractContextManager[None]],
        selected_profile: Callable[[], str | None],
    ) -> None:
        self.db = connection
        self.transaction = transaction
        self.selected_profile = selected_profile

    def reject_changed_input(
        self,
        *,
        chat_id: int,
        message_id: int,
        thread_id: int,
        topic_id: int,
        session_id: str | None = None,
        session_generation: int | None = None,
    ) -> PermissionInputDisposition:
        """Commit a refusal and receipt together, or leave compatible input untouched.

        The existing Hub input-notice outbox is a transport only. No blocker
        identity, job, held work or release callback is created here.
        """
        with self.transaction():
            if (
                self.db.execute(
                    "SELECT 1 FROM observed_messages WHERE chat_id=? AND message_id=?",
                    (chat_id, message_id),
                ).fetchone()
                is not None
            ):
                return "duplicate"
            topic = self.db.execute(
                "SELECT chat_id,thread_id FROM topics WHERE topic_id=?", (topic_id,)
            ).fetchone()
            if topic is None or topic["chat_id"] != chat_id or topic["thread_id"] != thread_id:
                raise StateError("permission input topic changed")
            session = self.db.execute(
                """SELECT session_id,generation,codex_permission_profile,writer_mode FROM agent_sessions
                   WHERE topic_id=? AND agent_id='codex' AND status IN ('active','satellite')""",
                (topic_id,),
            ).fetchone()
            if session_id is not None and (
                session is None
                or session["session_id"] != session_id
                or session["generation"] != session_generation
            ):
                raise StateError("permission input session snapshot changed")
            if session is None or session["codex_permission_profile"] == self.selected_profile():
                return None
            now = datetime.now(timezone.utc).isoformat()
            writer_hint = (
                "Сначала закройте локальный CLI и выполните /return. "
                if session["writer_mode"] == "local"
                else "Сначала выполните /release. "
                if session["writer_mode"] == "terminal"
                else ""
            )
            self.db.execute(
                """INSERT INTO hub_blocker_outbox
                   (outbox_id,event_key,kind,chat_id,thread_id,reply_to_message_id,
                    telegram_html,status,available_at,created_at,updated_at)
                   VALUES (?,?,'rejected',?,?,?,?,'pending',?,?,?)""",
                (
                    str(uuid.uuid4()),
                    f"{CodexPermissionSelectionChanged.code}:{chat_id}:{message_id}",
                    chat_id,
                    topic["thread_id"],
                    message_id,
                    "Настройка разрешений Codex изменилась. Этот запрос не передан "
                    "провайдеру и не будет запущен автоматически. Выберите Codex "
                    "через /agent codex. "
                    + writer_hint
                    + "Выполните /new и отправьте запрос снова.",
                    now,
                    now,
                    now,
                ),
            )
            self.db.execute(
                """INSERT INTO observed_messages
                   (chat_id,message_id,observer_agent_id,observed_at) VALUES (?,?,'hub',?)""",
                (chat_id, message_id, now),
            )
            return "rejected"
