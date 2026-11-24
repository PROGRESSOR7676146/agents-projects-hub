"""Durable agent mail: routes, letters and their Telegram deliveries (ADR 0112).

The external sender projects configured routes into the database at start, so
every worker offers and checks the same addresses. A worker validates a letter
before its result commit; the letter row, with its exact route, and its pending
display commit inside that result transaction. The sender settles each display
or failure notice with evidence: ``sending`` is saved before any HTTP request,
an attempted send without a processable receipt becomes ``unknown`` and is
never repeated, and only a proven rejection before sending may retry. A display
whose exact route is no longer configured is rejected before sending, and
settlement continues while no route is configured. A letter never starts a
provider turn.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal, Sequence

from .agent_letter_records import (
    ROUTE_AUTHORIZED_SQL,
    AgentLetterDraft,
    letter_time,
    route_parameters,
)
from .agent_mail import (
    MAX_LETTER_BYTES,
    MailRoute,
    contacts_prompt,
    decode_letter_body,
    ends_with_choices,
    extract_letter,
    failure_notice_html,
    letter_address,
    letter_caption,
    letter_file_allowed,
    queued_notice,
    refused_notice,
    topic_label,
)
from .artifacts import ValidatedArtifact
from .state import HubState, ProviderJobRecord

LETTER_SEND_LEASE_SECONDS = 120  # far above the 60 s document upload timeout
MAX_LETTER_ATTEMPTS = 5
DeliveryKind = Literal["display", "failure_notice"]


@dataclass(frozen=True, slots=True)
class LetterOutcome:
    text: str
    notice: str = ""
    draft: AgentLetterDraft | None = None


@dataclass(frozen=True, slots=True)
class LetterDelivery:
    letter_id: str
    kind: DeliveryKind
    chat_id: int
    thread_id: int
    lease_token: str
    attempts: int
    route: MailRoute
    file_name: str
    body: str
    caption_html: str
    failure_html: str

    @property
    def project_id(self) -> str:
        """The project that must currently own this delivery's topic."""
        if self.kind == "display":
            return self.route.target_project_id
        return self.route.source_project_id


@dataclass(frozen=True, slots=True)
class DisplayedLetter:
    letter_id: str
    file_name: str
    source_label: str
    source_agent_id: str
    target_project_id: str
    file_id: str
    file_unique_id: str
    byte_size: int


class AgentLetterStore:
    """Short immediate transactions over agent mail tables only."""

    def __init__(self, state: HubState, *, agent_name: str = "") -> None:
        self._state = state
        self._connection = state._connection
        self._agent_name = agent_name

    # Routes -----------------------------------------------------------------

    def sync_routes(self, routes: Sequence[MailRoute]) -> None:
        """Replace the projection with the configured routes in one transaction."""
        with self._state._immediate_transaction():
            self._connection.execute("DELETE FROM agent_mail_routes")
            self._connection.executemany(
                """INSERT INTO agent_mail_routes
                   (alias, source_project_id, source_chat_id, source_thread_id,
                    target_project_id, target_chat_id, target_thread_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [route_parameters(route) for route in routes],
            )

    def routes_for_topic(self, topic_id: int) -> tuple[MailRoute, ...]:
        rows = self._connection.execute(
            """SELECT routes.* FROM agent_mail_routes routes
               JOIN topics ON topics.chat_id = routes.source_chat_id
                AND topics.thread_id = routes.source_thread_id
                AND topics.project_id = routes.source_project_id
               WHERE topics.topic_id = ? ORDER BY routes.alias""",
            (topic_id,),
        ).fetchall()
        return tuple(_route(row) for row in rows)

    def _target_topic(self, route: MailRoute) -> sqlite3.Row | None:
        return self._connection.execute(
            """SELECT topic_id, title FROM topics
               WHERE chat_id = ? AND thread_id = ? AND project_id = ?""",
            (route.target_chat_id, route.target_thread_id, route.target_project_id),
        ).fetchone()

    def contacts_prompt(self, job: ProviderJobRecord) -> str:
        """Addresses for this job's topic; targets Hub has not seen are omitted."""
        contacts = []
        for route in self.routes_for_topic(job.topic_id):
            target = self._target_topic(route)
            if target is not None:
                label = topic_label(route.target_project_id, str(target["title"]))
                contacts.append((route, f"agent in {label}"))
        return contacts_prompt(job.job_id, tuple(contacts))

    # Worker preparation -----------------------------------------------------

    def prepare(
        self,
        job: ProviderJobRecord,
        text: str,
        artifacts: Sequence[ValidatedArtifact],
    ) -> LetterOutcome:
        """Strip a valid letter line and validate the letter before the commit."""
        extracted = extract_letter(text)
        if extracted.malformed:
            return LetterOutcome(text, refused_notice("строка письма не распознана"))
        directive = extracted.directive
        if directive is None:
            return LetterOutcome(text)
        if ends_with_choices(extracted.text):
            return LetterOutcome(
                extracted.text,
                refused_notice("письмо и кнопки выбора в одном ответе не поддерживаются"),
            )
        # The address binds the whole route: a retargeted route no longer matches.
        route = next(
            (
                item
                for item in self.routes_for_topic(job.topic_id)
                if letter_address(job.job_id, item) == directive.address
            ),
            None,
        )
        if route is None:
            return LetterOutcome(extracted.text, refused_notice("адрес не из списка этого хода"))
        target = self._target_topic(route)
        if target is None:
            return LetterOutcome(extracted.text, refused_notice("Hub ещё не знает тему получателя"))
        refusal, body = _letter_body(directive.file_name, artifacts)
        if body is None:
            return LetterOutcome(extracted.text, refused_notice(refusal))
        source = self._connection.execute(
            "SELECT project_id, title FROM topics WHERE topic_id = ?", (job.topic_id,)
        ).fetchone()
        source_label = topic_label(str(source["project_id"]), str(source["title"]))
        target_label = topic_label(route.target_project_id, str(target["title"]))
        draft = AgentLetterDraft(
            letter_id=uuid.uuid4().hex,
            route=route,
            target_topic_id=int(target["topic_id"]),
            file_name=directive.file_name,
            body=body,
            body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
            caption_html=letter_caption(
                source_label=source_label,
                agent_name=self._agent_name,
                file_name=directive.file_name,
                body=body,
            ),
            source_label=source_label,
        )
        notice = queued_notice(
            file_name=directive.file_name, alias=route.alias, target_label=target_label
        )
        return LetterOutcome(extracted.text, notice, draft)

    # Sender ------------------------------------------------------------------

    def recover_stale(self, *, now: datetime) -> int:
        """An expired send lease may have reached Telegram: mark it unknown."""
        current = letter_time(now)
        stale = """SELECT letter_id, kind FROM agent_letter_deliveries
                   WHERE state='sending' AND lease_expires_at <= ?"""
        if self._connection.execute(stale, (current,)).fetchone() is None:
            return 0  # The common case takes no write lock.
        with self._state._immediate_transaction():
            rows = self._connection.execute(stale, (current,)).fetchall()
            for row in rows:
                self._settle(
                    str(row["letter_id"]), row["kind"], "unknown", "lease_expired", current
                )
        return len(rows)

    def claim_next(self, *, now: datetime) -> LetterDelivery | None:
        """Save one due delivery as ``sending`` before any HTTP request."""
        current = letter_time(now)
        token = uuid.uuid4().hex
        due = """SELECT letter_id, kind FROM agent_letter_deliveries
                 WHERE state='pending' AND available_at <= ?
                 ORDER BY kind='display', available_at LIMIT 1"""
        if self._connection.execute(due, (current,)).fetchone() is None:
            return None  # The common case takes no write lock.
        with self._state._immediate_transaction():
            row = self._connection.execute(due, (current,)).fetchone()
            if row is None:
                return None
            self._connection.execute(
                """UPDATE agent_letter_deliveries
                   SET state='sending', attempts=attempts+1, lease_token=?,
                       lease_expires_at=?, send_started_at=?
                   WHERE letter_id=? AND kind=? AND state='pending'""",
                (
                    token,
                    letter_time(now + timedelta(seconds=LETTER_SEND_LEASE_SECONDS)),
                    current,
                    row["letter_id"],
                    row["kind"],
                ),
            )
            claimed = self._connection.execute(
                """SELECT deliveries.*, letters.route_alias, letters.source_project_id,
                          letters.source_chat_id, letters.source_thread_id,
                          letters.target_project_id, letters.target_chat_id,
                          letters.target_thread_id, letters.file_name, letters.body,
                          letters.caption_html, target.title AS target_title,
                          (SELECT display.state FROM agent_letter_deliveries display
                           WHERE display.letter_id = deliveries.letter_id
                             AND display.kind = 'display') AS display_state
                   FROM agent_letter_deliveries deliveries
                   JOIN agent_letters letters USING(letter_id)
                   JOIN topics target ON target.topic_id = letters.target_topic_id
                   WHERE deliveries.letter_id=? AND deliveries.kind=?""",
                (row["letter_id"], row["kind"]),
            ).fetchone()
        route = MailRoute(
            alias=str(claimed["route_alias"]),
            source_project_id=str(claimed["source_project_id"]),
            source_chat_id=int(claimed["source_chat_id"]),
            source_thread_id=int(claimed["source_thread_id"]),
            target_project_id=str(claimed["target_project_id"]),
            target_chat_id=int(claimed["target_chat_id"]),
            target_thread_id=int(claimed["target_thread_id"]),
        )
        return LetterDelivery(
            letter_id=str(claimed["letter_id"]),
            kind=claimed["kind"],
            chat_id=int(claimed["chat_id"]),
            thread_id=int(claimed["thread_id"]),
            lease_token=token,
            attempts=int(claimed["attempts"]),
            route=route,
            file_name=str(claimed["file_name"]),
            body=str(claimed["body"]),
            caption_html=str(claimed["caption_html"]),
            failure_html=failure_notice_html(
                file_name=str(claimed["file_name"]),
                target_label=topic_label(route.target_project_id, str(claimed["target_title"])),
                unknown=claimed["display_state"] == "unknown",
            ),
        )

    def route_authorized(self, delivery: LetterDelivery) -> bool:
        """The letter's exact route is still configured with both endpoints bound."""
        return (
            self._connection.execute(
                ROUTE_AUTHORIZED_SQL, route_parameters(delivery.route)
            ).fetchone()
            is not None
        )

    def topic_bound(self, delivery: LetterDelivery) -> bool:
        """The delivery topic still belongs to the project the letter names for it."""
        return _topic_project(self._connection, delivery.chat_id, delivery.thread_id) == (
            delivery.project_id
        )

    def confirm(
        self,
        delivery: LetterDelivery,
        *,
        message_id: int,
        file_id: str | None,
        file_unique_id: str | None,
        now: datetime,
    ) -> None:
        with self._state._immediate_transaction():
            self._connection.execute(
                """UPDATE agent_letter_deliveries
                   SET state='confirmed', receipt_message_id=?, receipt_file_id=?,
                       receipt_file_unique_id=?, lease_token=NULL, lease_expires_at=NULL,
                       completed_at=?
                   WHERE letter_id=? AND kind=? AND state='sending' AND lease_token=?""",
                (
                    message_id,
                    file_id,
                    file_unique_id,
                    letter_time(now),
                    delivery.letter_id,
                    delivery.kind,
                    delivery.lease_token,
                ),
            )

    def retry(self, delivery: LetterDelivery, *, error_code: str, retry_at: datetime) -> bool:
        """Return a proven not-sent attempt to ``pending``; False when attempts ran out."""
        if delivery.attempts >= MAX_LETTER_ATTEMPTS:
            self.settle(delivery, "rejected", error_code, now=retry_at)
            return False
        with self._state._immediate_transaction():
            self._connection.execute(
                """UPDATE agent_letter_deliveries
                   SET state='pending', available_at=?, error_code=?, lease_token=NULL,
                       lease_expires_at=NULL
                   WHERE letter_id=? AND kind=? AND state='sending' AND lease_token=?""",
                (
                    letter_time(retry_at),
                    error_code[:64],
                    delivery.letter_id,
                    delivery.kind,
                    delivery.lease_token,
                ),
            )
        return True

    def settle(
        self,
        delivery: LetterDelivery,
        state: Literal["rejected", "unknown"],
        error_code: str,
        *,
        now: datetime,
        receipt_message_id: int | None = None,
    ) -> None:
        """Settle an owned attempt; a partial receipt is kept as evidence."""
        with self._state._immediate_transaction():
            owned = self._connection.execute(
                """SELECT 1 FROM agent_letter_deliveries
                   WHERE letter_id=? AND kind=? AND state='sending' AND lease_token=?""",
                (delivery.letter_id, delivery.kind, delivery.lease_token),
            ).fetchone()
            if owned is None:
                return
            if receipt_message_id is not None:
                self._connection.execute(
                    """UPDATE agent_letter_deliveries SET receipt_message_id=?
                       WHERE letter_id=? AND kind=? AND state='sending'""",
                    (receipt_message_id, delivery.letter_id, delivery.kind),
                )
            self._settle(delivery.letter_id, delivery.kind, state, error_code, letter_time(now))

    def _settle(self, letter_id: str, kind: str, state: str, error_code: str, current: str) -> None:
        self._connection.execute(
            """UPDATE agent_letter_deliveries
               SET state=?, error_code=?, lease_token=NULL, lease_expires_at=NULL,
                   completed_at=?
               WHERE letter_id=? AND kind=? AND state='sending'""",
            (state, error_code[:64], current, letter_id, kind),
        )
        if kind != "display":
            return
        # The source topic learns once that its queued letter was not shown.
        self._connection.execute(
            """INSERT OR IGNORE INTO agent_letter_deliveries
               (letter_id, kind, chat_id, thread_id, state, available_at)
               SELECT letter_id, 'failure_notice', source_chat_id, source_thread_id,
                      'pending', ?
               FROM agent_letters WHERE letter_id = ?""",
            (current, letter_id),
        )

    # Controller ------------------------------------------------------------

    def displayed_letter(
        self, letter_id: str, *, chat_id: int, thread_id: int, message_id: int
    ) -> DisplayedLetter | None:
        """The letter whose confirmed display is exactly this Hub message.

        The display topic must still belong to the letter's target project, so a
        rebound group cannot pass an old letter to another project's agent.
        """
        row = self._connection.execute(
            """SELECT letters.letter_id, letters.file_name, letters.source_label,
                      letters.source_agent_id, letters.target_project_id, letters.body,
                      deliveries.receipt_file_id, deliveries.receipt_file_unique_id
               FROM agent_letter_deliveries deliveries
               JOIN agent_letters letters USING(letter_id)
               WHERE deliveries.letter_id = ? AND deliveries.kind = 'display'
                 AND deliveries.state = 'confirmed' AND deliveries.chat_id = ?
                 AND deliveries.thread_id = ? AND deliveries.receipt_message_id = ?""",
            (letter_id, chat_id, thread_id, message_id),
        ).fetchone()
        if row is None or _topic_project(self._connection, chat_id, thread_id) != str(
            row["target_project_id"]
        ):
            return None
        return DisplayedLetter(
            letter_id=str(row["letter_id"]),
            file_name=str(row["file_name"]),
            source_label=str(row["source_label"]),
            source_agent_id=str(row["source_agent_id"]),
            target_project_id=str(row["target_project_id"]),
            file_id=str(row["receipt_file_id"]),
            file_unique_id=str(row["receipt_file_unique_id"]),
            byte_size=len(str(row["body"]).encode("utf-8")),
        )


def _topic_project(connection: sqlite3.Connection, chat_id: int, thread_id: int) -> str | None:
    row = connection.execute(
        "SELECT project_id FROM topics WHERE chat_id = ? AND thread_id = ?",
        (chat_id, thread_id),
    ).fetchone()
    return None if row is None else str(row["project_id"])


def _route(row: sqlite3.Row) -> MailRoute:
    return MailRoute(
        alias=str(row["alias"]),
        source_project_id=str(row["source_project_id"]),
        source_chat_id=int(row["source_chat_id"]),
        source_thread_id=int(row["source_thread_id"]),
        target_project_id=str(row["target_project_id"]),
        target_chat_id=int(row["target_chat_id"]),
        target_thread_id=int(row["target_thread_id"]),
    )


def _letter_body(file_name: str, artifacts: Sequence[ValidatedArtifact]) -> tuple[str, str | None]:
    if not letter_file_allowed(file_name):
        return "письмо должно быть файлом .md или .txt", None
    matches = [artifact for artifact in artifacts if artifact.name == file_name]
    if len(matches) != 1:
        return f"файл «{file_name}» не найден среди вложений этого ответа", None
    artifact = matches[0]
    if artifact.size > MAX_LETTER_BYTES:
        return "нужен непустой текст UTF-8 не больше 64 КБ", None
    try:
        with artifact.path.open("rb") as stream:
            content = stream.read(MAX_LETTER_BYTES + 1)
    except OSError:
        return "файл письма недоступен", None
    if artifact.sha256 and hashlib.sha256(content).hexdigest() != artifact.sha256:
        return "файл письма изменился после проверки", None
    body = decode_letter_body(content)
    if body is None:
        return "нужен непустой текст UTF-8 не больше 64 КБ", None
    return "", body
