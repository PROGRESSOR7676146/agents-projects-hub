"""Read persistent execution-scope blockers inside a HubState transaction."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal, cast


@dataclass(frozen=True, slots=True)
class RootBlocker:
    kind: Literal["local", "terminal", "uncertain"]
    topic_id: int
    chat_id: int
    thread_id: int
    session_id: str | None
    generation: int | None
    cause_job_id: str | None


def persistent_root_blocker(connection: sqlite3.Connection, *, topic_id: int) -> RootBlocker | None:
    """Find a durable writer/uncertainty on this exact execution scope.

    The caller owns an immediate transaction. Topic titles are deliberately not
    returned: old rows can contain synthetic fallback names.
    """
    topic = connection.execute(
        """SELECT COALESCE(execution_scope, 'project:' || project_id) AS scope
           FROM topics WHERE topic_id = ?""",
        (topic_id,),
    ).fetchone()
    if topic is None:
        return None
    scope = str(topic["scope"])
    writer = connection.execute(
        """SELECT sessions.writer_mode, sessions.session_id, sessions.generation,
                  topics.topic_id, topics.chat_id, topics.thread_id
           FROM agent_sessions sessions
           JOIN topics ON topics.topic_id = sessions.topic_id
           WHERE sessions.status IN ('active', 'satellite')
             AND sessions.writer_mode IN ('local', 'terminal')
             AND COALESCE(topics.execution_scope, 'project:' || topics.project_id) = ?
           ORDER BY sessions.updated_at, sessions.session_id LIMIT 1""",
        (scope,),
    ).fetchone()
    if writer is not None:
        return RootBlocker(
            cast(Literal["local", "terminal"], str(writer["writer_mode"])),
            int(writer["topic_id"]),
            int(writer["chat_id"]),
            int(writer["thread_id"]),
            str(writer["session_id"]),
            int(writer["generation"]),
            None,
        )
    uncertain = connection.execute(
        """SELECT jobs.job_id, topics.topic_id, topics.chat_id, topics.thread_id
           FROM provider_jobs jobs
           JOIN topics ON topics.topic_id = jobs.topic_id
           WHERE jobs.status = 'indeterminate'
             AND COALESCE(topics.execution_scope, 'project:' || topics.project_id) = ?
             AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions r
                             WHERE r.job_id = jobs.job_id)
             AND NOT EXISTS (SELECT 1 FROM provider_turn_terminal_evidence e
                             WHERE e.job_id = jobs.job_id)
           ORDER BY jobs.created_at, jobs.job_id LIMIT 1""",
        (scope,),
    ).fetchone()
    if uncertain is None:
        return None
    return RootBlocker(
        "uncertain",
        int(uncertain["topic_id"]),
        int(uncertain["chat_id"]),
        int(uncertain["thread_id"]),
        None,
        None,
        str(uncertain["job_id"]),
    )


@dataclass(frozen=True, slots=True)
class RootBlockerNotice:
    outbox_id: str
    event_key: str
    kind: Literal["rejected", "held", "released"]
    chat_id: int
    thread_id: int
    reply_to_message_id: int | None
    telegram_html: str
    reply_markup: dict[str, object] | None
    blocker_topic_id: int | None
    blocker_session_id: str | None
    blocker_generation: int | None
    blocker_kind: str | None
    job_id: str | None
    status: str
    attempt_count: int
    lease_token: str | None
    telegram_message_id: int | None


def _notice(row: sqlite3.Row) -> RootBlockerNotice:
    markup = row["reply_markup_json"]
    return RootBlockerNotice(
        outbox_id=str(row["outbox_id"]),
        event_key=str(row["event_key"]),
        kind=cast(Literal["rejected", "held", "released"], str(row["kind"])),
        chat_id=int(row["chat_id"]),
        thread_id=int(row["thread_id"]),
        reply_to_message_id=(
            None if row["reply_to_message_id"] is None else int(row["reply_to_message_id"])
        ),
        telegram_html=str(row["telegram_html"]),
        reply_markup=json.loads(str(markup)) if markup is not None else None,
        blocker_topic_id=(
            None if row["blocker_topic_id"] is None else int(row["blocker_topic_id"])
        ),
        blocker_session_id=(
            None if row["blocker_session_id"] is None else str(row["blocker_session_id"])
        ),
        blocker_generation=(
            None if row["blocker_generation"] is None else int(row["blocker_generation"])
        ),
        blocker_kind=(None if row["blocker_kind"] is None else str(row["blocker_kind"])),
        job_id=None if row["job_id"] is None else str(row["job_id"]),
        status=str(row["status"]),
        attempt_count=int(row["attempt_count"]),
        lease_token=None if row["lease_token"] is None else str(row["lease_token"]),
        telegram_message_id=(
            None if row["telegram_message_id"] is None else int(row["telegram_message_id"])
        ),
    )


def _topic_link(chat_id: int, thread_id: int) -> str | None:
    raw = str(chat_id)
    if not raw.startswith("-100") or thread_id <= 0:
        return None
    suffix = raw[4:]
    if not suffix.isdigit() or not suffix:
        return None
    return f"https://t.me/c/{suffix}/{thread_id}"


def _blocker_text(
    blocker: RootBlocker, *, same_topic: bool
) -> tuple[str, dict[str, object] | None]:
    link = _topic_link(blocker.chat_id, blocker.thread_id)
    owner = (
        f'<a href="{link}">теме с владельцем проекта</a>' if link else "теме с владельцем проекта"
    )
    if blocker.kind == "uncertain":
        text = (
            "Провайдер не получил этот запрос: на этом проекте ещё не подтверждён исход "
            f"предыдущего хода в {owner}. Новый запрос не сохранён для автоматического "
            "запуска. Проверьте состояние в теме-владельце и затем повторите запрос."
        )
        label = "Открыть тему-владельца"
    else:
        command = "/return" if blocker.kind == "local" else "/release"
        if same_topic:
            text = (
                "Провайдер не получил этот запрос: эта сессия закреплена за локальным "
                "клиентом. Hub не может определить, открыт ли он. Если работа закончена, "
                f"закройте клиент и выполните {command} в этой теме. Этот запрос не "
                "будет выполнен автоматически."
            )
        else:
            text = (
                "Провайдер не получил этот запрос: проект сейчас закреплён за "
                f"локальной сессией в {owner}. Hub не может определить, открыт ли CLI. "
                f"Если работа закончена, закройте CLI и выполните {command} там. "
                "Этот запрос не будет выполнен автоматически; после освобождения "
                "проекта повторите его."
            )
        label = "Освободить проект"
    markup: dict[str, object] | None = (
        {"inline_keyboard": [[{"text": label, "url": link}]]} if link else None
    )
    return text, markup


class RootBlockerState:
    """HubState-owned durable blocker dispositions and sender leases."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        transaction: Callable[[], AbstractContextManager[None]],
        state_error: type[Exception],
    ) -> None:
        self.db = connection
        self.transaction = transaction
        self.state_error = state_error

    def reject_blocked_input(
        self,
        *,
        chat_id: int,
        message_id: int,
        topic_id: int,
        session_id: str,
        session_generation: int,
    ) -> RootBlockerNotice | None:
        """Atomically claim a blocked input and prepare one Hub-owned reply."""
        event_key = f"rejected:{chat_id}:{message_id}"
        with self.transaction():
            existing = self.db.execute(
                "SELECT * FROM hub_blocker_outbox WHERE event_key = ?", (event_key,)
            ).fetchone()
            if existing is not None:
                return _notice(existing)
            observed = self.db.execute(
                "SELECT 1 FROM observed_messages WHERE chat_id=? AND message_id=?",
                (chat_id, message_id),
            ).fetchone()
            if observed is not None:
                return None
            topic = self.db.execute(
                "SELECT chat_id, thread_id FROM topics WHERE topic_id=?", (topic_id,)
            ).fetchone()
            session = self.db.execute(
                """SELECT topic_id, generation, status FROM agent_sessions
                   WHERE session_id=?""",
                (session_id,),
            ).fetchone()
            if (
                topic is None
                or int(topic["chat_id"]) != chat_id
                or session is None
                or int(session["topic_id"]) != topic_id
                or int(session["generation"]) != session_generation
                or str(session["status"]) not in {"active", "satellite"}
            ):
                raise self.state_error("blocked input session snapshot changed")
            blocker = persistent_root_blocker(self.db, topic_id=topic_id)
            if blocker is None:
                return None
            text, markup = _blocker_text(blocker, same_topic=blocker.topic_id == topic_id)
            now = datetime.now(timezone.utc).isoformat()
            outbox_id = str(uuid.uuid4())
            self.db.execute(
                """INSERT INTO hub_blocker_outbox
                   (outbox_id,event_key,kind,chat_id,thread_id,reply_to_message_id,
                    telegram_html,reply_markup_json,blocker_topic_id,blocker_session_id,
                    blocker_generation,blocker_kind,blocker_job_id,
                    status,available_at,created_at,updated_at)
                   VALUES (?,?,'rejected',?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
                (
                    outbox_id,
                    event_key,
                    chat_id,
                    int(topic["thread_id"]),
                    message_id,
                    text,
                    json.dumps(markup, ensure_ascii=False) if markup else None,
                    blocker.topic_id,
                    blocker.session_id,
                    blocker.generation,
                    blocker.kind,
                    blocker.cause_job_id,
                    now,
                    now,
                    now,
                ),
            )
            self.db.execute(
                """INSERT INTO observed_messages
                   (chat_id,message_id,observer_agent_id,observed_at)
                   VALUES (?,?,'hub',?)""",
                (chat_id, message_id, now),
            )
            row = self.db.execute(
                "SELECT * FROM hub_blocker_outbox WHERE outbox_id=?", (outbox_id,)
            ).fetchone()
            assert row is not None
            return _notice(row)

    def materialize_held_jobs(self) -> int:
        """Pause legacy queued work behind persistent root owners, once per job.

        Sender polling and writer return serialize on the same SQLite writer lock.
        No provider operation occurs here.
        """
        with self.transaction():
            rows = self.db.execute(
                """SELECT jobs.job_id, jobs.chat_id, jobs.message_id, jobs.topic_id,
                          topics.thread_id
                   FROM provider_jobs jobs JOIN topics ON topics.topic_id=jobs.topic_id
                   WHERE jobs.status IN ('queued','retry_wait')
                     AND NOT EXISTS (SELECT 1 FROM provider_job_holds holds
                                     WHERE holds.job_id=jobs.job_id)
                   ORDER BY jobs.created_at, jobs.topic_id, jobs.topic_sequence"""
            ).fetchall()
            count = 0
            for row in rows:
                blocker = persistent_root_blocker(self.db, topic_id=int(row["topic_id"]))
                if blocker is None:
                    continue
                self._hold_job(row, blocker)
                count += 1
            older = self.db.execute(
                """SELECT jobs.job_id,jobs.chat_id,jobs.message_id,jobs.topic_id,
                          destination.thread_id,owner.topic_id AS owner_topic_id,
                          owner.chat_id AS owner_chat_id,owner.thread_id AS owner_thread_id,
                          holds.cause_job_id
                   FROM provider_job_holds holds
                   JOIN provider_jobs jobs ON jobs.job_id=holds.job_id
                   JOIN topics destination ON destination.topic_id=jobs.topic_id
                   JOIN provider_jobs cause ON cause.job_id=holds.cause_job_id
                   JOIN topics owner ON owner.topic_id=cause.topic_id
                   WHERE holds.hold_reason='uncertainty' AND holds.decision='pending'
                     AND jobs.status IN ('queued','retry_wait')
                     AND NOT EXISTS (SELECT 1 FROM hub_blocker_outbox notices
                                     WHERE notices.event_key='held:'||jobs.job_id)"""
            ).fetchall()
            for row in older:
                self._hold_job(
                    row,
                    RootBlocker(
                        "uncertain",
                        int(row["owner_topic_id"]),
                        int(row["owner_chat_id"]),
                        int(row["owner_thread_id"]),
                        None,
                        None,
                        str(row["cause_job_id"]),
                    ),
                )
                count += 1
            return count

    def held_count_for_topic(self, topic_id: int) -> int:
        return int(
            self.db.execute(
                """SELECT COUNT(*) FROM provider_job_holds holds
               JOIN provider_jobs jobs ON jobs.job_id=holds.job_id
               WHERE jobs.topic_id=? AND jobs.status IN ('queued','retry_wait')
                 AND holds.decision='pending'""",
                (topic_id,),
            ).fetchone()[0]
        )

    def _hold_job(self, row: sqlite3.Row, blocker: RootBlocker) -> None:
        job_id = str(row["job_id"])
        now = datetime.now(timezone.utc).isoformat()
        inserted = self.db.execute(
            """INSERT OR IGNORE INTO provider_job_holds
               (job_id,cause_job_id,held_at,hold_reason)
               VALUES (?,?,?,?)""",
            (
                job_id,
                blocker.cause_job_id or job_id,
                now,
                "uncertainty" if blocker.kind == "uncertain" else blocker.kind,
            ),
        )
        if (
            inserted.rowcount != 1
            and self.db.execute(
                "SELECT 1 FROM provider_job_holds WHERE job_id=?", (job_id,)
            ).fetchone()
            is None
        ):
            raise self.state_error("provider job hold was not recorded")
        link = _topic_link(blocker.chat_id, blocker.thread_id)
        owner = f'<a href="{link}">тема-владелец</a>' if link else "тема-владелец"
        text = (
            (
                f"Запрос сохранён, но не начат: прежний ход в {owner} оставил очередь на паузе. "
                if blocker.kind == "uncertain"
                else f"Запрос сохранён, но не начат: проект удерживает {owner}. "
            )
            + "Он не запустится автоматически после освобождения проекта. "
            "Отмените этот запрос или явно подтвердите его запуск после освобождения."
        )
        markup: dict[str, object] = {
            "inline_keyboard": [
                [
                    {"text": "Подтвердить запуск", "callback_data": f"bh:c:{job_id}"},
                    {"text": "Отменить запрос", "callback_data": f"bh:x:{job_id}"},
                ]
            ]
        }
        if link:
            markup["inline_keyboard"].append([{"text": "Освободить проект", "url": link}])  # type: ignore[union-attr]
        notice = self.db.execute(
            """INSERT OR IGNORE INTO hub_blocker_outbox
               (outbox_id,event_key,kind,chat_id,thread_id,reply_to_message_id,
                telegram_html,reply_markup_json,blocker_topic_id,blocker_session_id,
                blocker_generation,blocker_kind,blocker_job_id,job_id,
                status,available_at,created_at,updated_at)
               VALUES (?,?,'held',?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?)""",
            (
                str(uuid.uuid4()),
                f"held:{job_id}",
                int(row["chat_id"]),
                int(row["thread_id"]),
                int(row["message_id"]),
                text,
                json.dumps(markup, ensure_ascii=False),
                blocker.topic_id,
                blocker.session_id,
                blocker.generation,
                blocker.kind,
                blocker.cause_job_id,
                job_id,
                now,
                now,
                now,
            ),
        )
        if (
            notice.rowcount != 1
            and self.db.execute(
                "SELECT 1 FROM hub_blocker_outbox WHERE event_key=?", (f"held:{job_id}",)
            ).fetchone()
            is None
        ):
            raise self.state_error("held-job notice was not recorded")

    def hold_scope_before_return(self, topic_id: int) -> None:
        """Called inside the owner session's existing immediate transaction."""
        rows = self.db.execute(
            """SELECT jobs.job_id,jobs.chat_id,jobs.message_id,jobs.topic_id,
                      topics.thread_id FROM provider_jobs jobs
               JOIN topics ON topics.topic_id=jobs.topic_id
               JOIN topics owner ON owner.topic_id=?
               WHERE COALESCE(topics.execution_scope,'project:'||topics.project_id)=
                     COALESCE(owner.execution_scope,'project:'||owner.project_id)
                 AND jobs.status IN ('queued','retry_wait')
                 AND NOT EXISTS (SELECT 1 FROM provider_job_holds holds
                                 WHERE holds.job_id=jobs.job_id)""",
            (topic_id,),
        ).fetchall()
        blocker = persistent_root_blocker(self.db, topic_id=topic_id)
        if blocker is None:
            raise self.state_error("local writer vanished before queue hold")
        for row in rows:
            self._hold_job(row, blocker)

    def notice_released_scope(self, *, session_id: str, generation: int) -> None:
        """Called in the same transaction that returns a local writer."""
        rows = self.db.execute(
            """SELECT recipient.topic_id, recipient.chat_id, recipient.thread_id,
                      MAX(CASE WHEN notices.kind='rejected' THEN 1 ELSE 0 END)
                        AS had_rejected
               FROM hub_blocker_outbox notices
               JOIN topics recipient ON recipient.chat_id=notices.chat_id
                                    AND recipient.thread_id=notices.thread_id
               WHERE notices.blocker_session_id=? AND notices.blocker_generation=?
                 AND notices.kind IN ('rejected','held')
               GROUP BY recipient.topic_id,recipient.chat_id,recipient.thread_id""",
            (session_id, generation),
        ).fetchall()
        now = datetime.now(timezone.utc).isoformat()
        for row in rows:
            topic_id = int(row["topic_id"])
            held = int(
                self.db.execute(
                    """SELECT COUNT(*) FROM provider_job_holds holds
                   JOIN provider_jobs jobs ON jobs.job_id=holds.job_id
                   WHERE jobs.topic_id=? AND jobs.status IN ('queued','retry_wait')
                     AND holds.decision='pending'""",
                    (topic_id,),
                ).fetchone()[0]
            )
            if held:
                text = (
                    "Блокировка проекта снята. Сохранённый запрос остаётся на паузе: "
                    "подтвердите его запуск или отмените в уведомлении выше."
                )
            elif int(row["had_rejected"]):
                text = (
                    "Блокировка проекта снята. Отклонённый запрос не был передан провайдеру; "
                    "если он ещё нужен, отправьте его снова."
                )
            else:
                text = (
                    "Блокировка проекта снята. Ранее сохранённый запрос отменён; "
                    "если он нужен, отправьте новый запрос."
                )
            self.db.execute(
                """INSERT OR IGNORE INTO hub_blocker_outbox
                   (outbox_id,event_key,kind,chat_id,thread_id,telegram_html,
                    blocker_topic_id,blocker_session_id,blocker_generation,status,
                    available_at,created_at,updated_at)
                   VALUES (?,?,'released',?,?,?, ?,?,?,'pending',?,?,?)""",
                (
                    str(uuid.uuid4()),
                    f"released:{session_id}:{generation}:{topic_id}",
                    int(row["chat_id"]),
                    int(row["thread_id"]),
                    text,
                    topic_id,
                    session_id,
                    generation,
                    now,
                    now,
                    now,
                ),
            )

    def materialize_released_uncertainty(self) -> int:
        """Notify prior recipients once exact uncertainty no longer excludes the root."""
        with self.transaction():
            rows = self.db.execute(
                """SELECT notices.blocker_job_id,recipient.topic_id,
                          recipient.chat_id,recipient.thread_id,
                          MAX(CASE WHEN notices.kind='rejected' THEN 1 ELSE 0 END)
                            AS had_rejected
                   FROM hub_blocker_outbox notices
                   JOIN provider_jobs source ON source.job_id=notices.blocker_job_id
                   JOIN topics recipient ON recipient.chat_id=notices.chat_id
                                        AND recipient.thread_id=notices.thread_id
                   WHERE notices.blocker_kind='uncertain'
                     AND notices.kind IN ('rejected','held')
                     AND (source.status IN ('completed','failed','cancelled')
                          OR EXISTS (SELECT 1 FROM provider_job_resolutions r
                                     WHERE r.job_id=source.job_id)
                          OR EXISTS (SELECT 1 FROM provider_turn_terminal_evidence e
                                     WHERE e.job_id=source.job_id))
                   GROUP BY notices.blocker_job_id,recipient.topic_id,
                            recipient.chat_id,recipient.thread_id"""
            ).fetchall()
            now = datetime.now(timezone.utc).isoformat()
            created = 0
            for row in rows:
                topic_id = int(row["topic_id"])
                if persistent_root_blocker(self.db, topic_id=topic_id) is not None:
                    continue
                held = self.held_count_for_topic(topic_id)
                if held:
                    text = (
                        "Исход прежнего хода подтверждён. Сохранённый запрос остаётся "
                        "на паузе: подтвердите запуск или отмените в уведомлении выше."
                    )
                elif int(row["had_rejected"]):
                    text = (
                        "Ограничение проекта снято. Отклонённый запрос не был передан "
                        "провайдеру; если он ещё нужен, отправьте его снова."
                    )
                else:
                    text = (
                        "Ограничение проекта снято. Ранее сохранённый запрос отменён; "
                        "если он нужен, отправьте новый запрос."
                    )
                cursor = self.db.execute(
                    """INSERT OR IGNORE INTO hub_blocker_outbox
                       (outbox_id,event_key,kind,chat_id,thread_id,telegram_html,
                        blocker_topic_id,blocker_kind,blocker_job_id,status,
                        available_at,created_at,updated_at)
                       VALUES (?,?,'released',?,?,?,?,'uncertain',?,'pending',?,?,?)""",
                    (
                        str(uuid.uuid4()),
                        f"released:uncertain:{row['blocker_job_id']}:{topic_id}",
                        int(row["chat_id"]),
                        int(row["thread_id"]),
                        text,
                        topic_id,
                        str(row["blocker_job_id"]),
                        now,
                        now,
                        now,
                    ),
                )
                created += cursor.rowcount
            return created

    def decide_held_job(
        self,
        *,
        job_id: str,
        action: Literal["confirm", "cancel"],
        chat_id: int,
        thread_id: int,
        notice_message_id: int,
    ) -> str:
        """Bind a callback to its delivered notice and exact original job."""
        with self.transaction():
            row = self.db.execute(
                """SELECT jobs.status, jobs.chat_id, topics.thread_id, holds.decision,
                          notice.telegram_message_id
                   FROM provider_jobs jobs
                   JOIN topics ON topics.topic_id=jobs.topic_id
                   JOIN provider_job_holds holds ON holds.job_id=jobs.job_id
                   JOIN hub_blocker_outbox notice ON notice.event_key='held:'||jobs.job_id
                   WHERE jobs.job_id=? AND notice.status='delivered'""",
                (job_id,),
            ).fetchone()
            if (
                row is None
                or int(row["chat_id"]) != chat_id
                or int(row["thread_id"]) != thread_id
                or row["telegram_message_id"] != notice_message_id
            ):
                raise self.state_error("held-job notice or topic changed")
            current = str(row["decision"])
            chosen = "confirmed" if action == "confirm" else "cancelled"
            if current == chosen:
                return current
            if current != "pending" or str(row["status"]) not in {"queued", "retry_wait"}:
                raise self.state_error("held job was already decided or started")
            if action == "confirm":
                topic = self.db.execute(
                    "SELECT topic_id FROM provider_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
                assert topic is not None
                if persistent_root_blocker(self.db, topic_id=int(topic["topic_id"])):
                    raise self.state_error("project is still held; release its writer first")
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                "UPDATE provider_job_holds SET decision=?,decided_at=? WHERE job_id=?",
                (chosen, now, job_id),
            )
            if action == "cancel":
                self.db.execute(
                    """UPDATE provider_jobs SET status='cancelled',next_attempt_at=NULL,
                       error_class='owner_decision',error_code='held_job_cancelled',updated_at=?
                       WHERE job_id=?""",
                    (now, job_id),
                )
            return chosen

    def lease_notice(
        self, sender_id: str, *, now: datetime | None = None
    ) -> RootBlockerNotice | None:
        current = (now or datetime.now(timezone.utc)).isoformat()
        with self.transaction():
            self.db.execute(
                """UPDATE hub_blocker_outbox SET status='pending',lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE status='sending' AND lease_expires_at<=?""",
                (current, current),
            )
            row = self.db.execute(
                """SELECT * FROM hub_blocker_outbox WHERE status='pending'
                   AND available_at<=? ORDER BY created_at,outbox_id LIMIT 1""",
                (current,),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            self.db.execute(
                """UPDATE hub_blocker_outbox SET status='sending',lease_owner=?,
                   lease_token=?,lease_expires_at=?,attempt_count=attempt_count+1,
                   updated_at=? WHERE outbox_id=?""",
                (
                    sender_id,
                    token,
                    (datetime.fromisoformat(current) + timedelta(seconds=90)).isoformat(),
                    current,
                    row["outbox_id"],
                ),
            )
            leased = self.db.execute(
                "SELECT * FROM hub_blocker_outbox WHERE outbox_id=?", (row["outbox_id"],)
            ).fetchone()
            assert leased is not None
            return _notice(leased)

    def complete_notice(self, notice: RootBlockerNotice, message_id: int) -> None:
        with self.transaction():
            self.db.execute(
                """UPDATE hub_blocker_outbox SET status='delivered',telegram_message_id=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                   delivered_at=?,updated_at=? WHERE outbox_id=? AND lease_token=?""",
                (
                    message_id,
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    notice.outbox_id,
                    notice.lease_token,
                ),
            )

    def retry_notice(self, notice: RootBlockerNotice, error_code: str, delay_seconds: int) -> None:
        now = datetime.now(timezone.utc)
        with self.transaction():
            self.db.execute(
                """UPDATE hub_blocker_outbox SET status='pending',available_at=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,error_code=?,
                   updated_at=? WHERE outbox_id=? AND lease_token=?""",
                (
                    (now + timedelta(seconds=delay_seconds)).isoformat(),
                    error_code[:80],
                    now.isoformat(),
                    notice.outbox_id,
                    notice.lease_token,
                ),
            )
