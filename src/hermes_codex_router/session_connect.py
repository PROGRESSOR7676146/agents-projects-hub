"""Durable, model-free orchestration for connecting saved Codex threads."""

from __future__ import annotations

import hashlib
import html
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .session_adoption_state import AdoptionRequest, CodexSessionOrigins
from .state import HubState, StateError, _now

WORKFLOW_TTL = timedelta(minutes=15)
WORKER_LEASE = timedelta(seconds=30)
OUTBOX_LEASE = timedelta(seconds=30)
MAX_CANDIDATES = 24


def _token() -> str:
    return secrets.token_hex(8)


def _deadline(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True, slots=True)
class ConnectCandidate:
    candidate_id: str
    provider_thread_id: str
    safe_label: str
    updated_at_epoch: int


@dataclass(frozen=True, slots=True)
class ConnectWorkflow:
    workflow_id: str
    owner_user_id: int
    entrypoint: str
    project_id: str | None
    canonical_root: Path | None
    source_thread_id: str | None
    source_label: str | None
    source_updated_at: int | None
    destination_chat_id: int | None
    destination_thread_id: int | None
    expected_session_id: str | None
    replaces_session_id: str | None
    model: str
    effort: str
    stage: str
    code_id: str | None
    lease_token: str | None
    result_session_id: str | None
    error_code: str | None
    expires_at: str


@dataclass(frozen=True, slots=True)
class ConnectOutbox:
    outbox_id: str
    workflow_id: str
    kind: str
    chat_id: int
    thread_id: int
    telegram_html: str
    reply_markup: dict[str, object] | None
    status: str
    lease_token: str | None
    attempt_count: int


class SessionConnectStore:
    """A narrow repository whose mutating transitions use immediate transactions."""

    def __init__(self, state: HubState) -> None:
        self.state = state
        self.connection = state._connection

    @staticmethod
    def _workflow(row: sqlite3.Row) -> ConnectWorkflow:
        return ConnectWorkflow(
            str(row["workflow_id"]),
            int(row["owner_user_id"]),
            str(row["entrypoint"]),
            None if row["project_id"] is None else str(row["project_id"]),
            None if row["canonical_root"] is None else Path(str(row["canonical_root"])),
            row["source_thread_id"],
            row["source_label"],
            None if row["source_updated_at"] is None else int(row["source_updated_at"]),
            None if row["destination_chat_id"] is None else int(row["destination_chat_id"]),
            None if row["destination_thread_id"] is None else int(row["destination_thread_id"]),
            row["expected_session_id"],
            row["replaces_session_id"],
            str(row["model"]),
            str(row["effort"]),
            str(row["stage"]),
            row["code_id"],
            row["lease_token"],
            row["result_session_id"],
            row["error_code"],
            str(row["expires_at"]),
        )

    @staticmethod
    def _outbox(row: sqlite3.Row) -> ConnectOutbox:
        markup = row["reply_markup_json"]
        parsed = json.loads(str(markup)) if markup is not None else None
        return ConnectOutbox(
            str(row["outbox_id"]),
            str(row["workflow_id"]),
            str(row["kind"]),
            int(row["chat_id"]),
            int(row["thread_id"]),
            str(row["telegram_html"]),
            parsed if isinstance(parsed, dict) else None,
            str(row["status"]),
            row["lease_token"],
            int(row["attempt_count"]),
        )

    def get(self, workflow_id: str) -> ConnectWorkflow:
        row = self.connection.execute(
            "SELECT * FROM session_connect_workflows WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise StateError("connect_workflow_unknown")
        return self._workflow(row)

    def active_for_owner(self, owner_user_id: int) -> ConnectWorkflow | None:
        row = self.connection.execute(
            """SELECT * FROM session_connect_workflows WHERE owner_user_id=?
               AND stage NOT IN ('completed','cancelled','expired','failed')
               ORDER BY created_at DESC LIMIT 1""",
            (owner_user_id,),
        ).fetchone()
        if row is None:
            return None
        workflow = self._workflow(row)
        if _parse_time(workflow.expires_at) <= datetime.now(timezone.utc):
            with self.state._immediate_transaction():
                self.connection.execute(
                    """UPDATE session_connect_workflows SET stage='expired', updated_at=?
                       WHERE workflow_id=? AND stage NOT IN ('completed','cancelled','failed')""",
                    (_now(), workflow.workflow_id),
                )
            return None
        return workflow

    def start_topic(
        self,
        *,
        owner_user_id: int,
        project_id: str,
        canonical_root: Path,
        chat_id: int,
        thread_id: int,
        model: str,
        effort: str,
        entrypoint: str = "topic",
        code_id: str | None = None,
        source: ConnectCandidate | None = None,
    ) -> ConnectWorkflow:
        if owner_user_id <= 0 or chat_id >= 0 or thread_id <= 0:
            raise StateError("invalid_connect_identity")
        topic = self.state.find_topic(chat_id, thread_id)
        if topic is None or topic.project_id != project_id:
            raise StateError("topic_mismatch")
        current = self.state.active_session(topic.topic_id)
        if current is not None and current.agent_id != "codex":
            raise StateError("active_provider_is_not_codex")
        if current is not None and current.writer_mode != "telegram":
            raise StateError("local_writer")
        workflow_id = _token()
        stage = "confirming" if source is not None else "discovering"
        now = _now()
        with self.state._immediate_transaction():
            self.connection.execute(
                """UPDATE session_connect_workflows SET stage='cancelled', updated_at=?
                   WHERE owner_user_id=? AND stage NOT IN
                   ('completed','cancelled','expired','failed','marker_unknown')""",
                (now, owner_user_id),
            )
            self.connection.execute(
                """INSERT INTO session_connect_workflows (
                   workflow_id,owner_user_id,entrypoint,project_id,canonical_root,
                   source_thread_id,source_label,source_updated_at,destination_chat_id,
                   destination_thread_id,expected_session_id,replaces_session_id,model,effort,
                   stage,code_id,expires_at,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    workflow_id,
                    owner_user_id,
                    entrypoint,
                    project_id,
                    str(canonical_root),
                    None if source is None else source.provider_thread_id,
                    None if source is None else source.safe_label,
                    None if source is None else source.updated_at_epoch,
                    chat_id,
                    thread_id,
                    None if current is None else current.session_id,
                    (
                        current.session_id
                        if current is not None and current.provider_session_id is not None
                        else None
                    ),
                    model,
                    effort,
                    stage,
                    code_id,
                    _deadline(WORKFLOW_TTL),
                    now,
                    now,
                ),
            )
        return self.get(workflow_id)

    def lease_worker(self, worker_id: str) -> ConnectWorkflow | None:
        now = datetime.now(timezone.utc)
        token = _token()
        with self.state._immediate_transaction():
            self.connection.execute(
                """UPDATE session_connect_workflows SET stage='expired',updated_at=?
                   WHERE expires_at<=? AND stage NOT IN
                   ('completed','cancelled','expired','failed','marker_unknown')""",
                (now.isoformat(), now.isoformat()),
            )
            row = self.connection.execute(
                """SELECT workflow_id FROM session_connect_workflows
                   WHERE stage IN ('discovering','activation_requested')
                   AND (lease_token IS NULL OR lease_expires_at<=?)
                   ORDER BY created_at LIMIT 1""",
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                return None
            cursor = self.connection.execute(
                """UPDATE session_connect_workflows SET lease_owner=?,lease_token=?,
                   lease_expires_at=?,updated_at=? WHERE workflow_id=?
                   AND (lease_token IS NULL OR lease_expires_at<=?)""",
                (
                    worker_id,
                    token,
                    (now + WORKER_LEASE).isoformat(),
                    now.isoformat(),
                    row["workflow_id"],
                    now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                return None
        return self.get(str(row["workflow_id"]))

    def finish_discovery(
        self,
        workflow_id: str,
        lease_token: str | None,
        candidates: tuple[ConnectCandidate, ...],
    ) -> tuple[ConnectCandidate, ...]:
        bounded = candidates[:MAX_CANDIDATES]
        now = _now()
        with self.state._immediate_transaction():
            workflow = self.get(workflow_id)
            if workflow.stage != "discovering" or workflow.lease_token != lease_token:
                raise StateError("connect_worker_lease_changed")
            self.connection.execute(
                "DELETE FROM session_connect_candidates WHERE workflow_id=?", (workflow_id,)
            )
            stored: list[ConnectCandidate] = []
            for item in bounded:
                candidate_id = item.candidate_id if 8 <= len(item.candidate_id) <= 32 else _token()
                safe_label = " ".join(item.safe_label.split())[:160] or "Сохранённая сессия"
                self.connection.execute(
                    """INSERT INTO session_connect_candidates
                       (candidate_id,workflow_id,provider_thread_id,safe_label,updated_at_epoch,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (
                        candidate_id,
                        workflow_id,
                        item.provider_thread_id,
                        safe_label,
                        item.updated_at_epoch,
                        now,
                    ),
                )
                stored.append(
                    ConnectCandidate(
                        candidate_id, item.provider_thread_id, safe_label, item.updated_at_epoch
                    )
                )
            if not stored:
                self.connection.execute(
                    """UPDATE session_connect_workflows SET stage='failed',error_code='no_sessions',
                       lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=?
                       WHERE workflow_id=?""",
                    (now, workflow_id),
                )
                self._insert_outbox_locked(
                    workflow_id,
                    "notice",
                    workflow.destination_chat_id or workflow.owner_user_id,
                    workflow.destination_thread_id or 1,
                    "Подходящих сохранённых Codex-сессий для этого проекта не найдено.",
                )
                return ()
            buttons: dict[str, object] = {
                "inline_keyboard": [
                    [{"text": item.safe_label, "callback_data": f"cx:s:{item.candidate_id}"}]
                    for item in stored
                ]
                + [[{"text": "Отмена", "callback_data": f"cx:x:{workflow_id}"}]]
            }
            self.connection.execute(
                """UPDATE session_connect_workflows SET stage='choosing_source',lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE workflow_id=?""",
                (now, workflow_id),
            )
            self._insert_outbox_locked(
                workflow_id,
                "source_menu",
                workflow.destination_chat_id or workflow.owner_user_id,
                workflow.destination_thread_id or 1,
                "Выберите сохранённый разговор Codex:",
                buttons,
            )
        return tuple(stored)

    def select_candidate(self, owner_user_id: int, candidate_id: str) -> ConnectWorkflow:
        with self.state._immediate_transaction():
            row = self.connection.execute(
                """SELECT c.*,w.owner_user_id,w.stage,w.destination_chat_id
                   FROM session_connect_candidates c JOIN session_connect_workflows w
                   ON w.workflow_id=c.workflow_id WHERE c.candidate_id=?""",
                (candidate_id,),
            ).fetchone()
            if (
                row is None
                or int(row["owner_user_id"]) != owner_user_id
                or str(row["stage"]) != "choosing_source"
            ):
                raise StateError("connect_selection_stale")
            stage = (
                "confirming" if row["destination_chat_id"] is not None else "choosing_destination"
            )
            self.connection.execute(
                """UPDATE session_connect_workflows SET source_thread_id=?,source_label=?,
                   source_updated_at=?,stage=?,updated_at=? WHERE workflow_id=?""",
                (
                    row["provider_thread_id"],
                    row["safe_label"],
                    row["updated_at_epoch"],
                    stage,
                    _now(),
                    row["workflow_id"],
                ),
            )
            workflow_id = str(row["workflow_id"])
        return self.get(workflow_id)

    def request_activation(self, owner_user_id: int, workflow_id: str) -> ConnectWorkflow:
        with self.state._immediate_transaction():
            workflow = self.get(workflow_id)
            if workflow.owner_user_id != owner_user_id or workflow.stage != "confirming":
                raise StateError("connect_confirmation_stale")
            if workflow.destination_chat_id is None or workflow.destination_thread_id is None:
                raise StateError("connect_destination_missing")
            request = self._adoption_request(workflow)
            target = CodexSessionOrigins(self.state).preview(request)
            current_id = target.session.session_id if target.session else None
            if current_id != workflow.expected_session_id and not target.already_attached:
                raise StateError("target_changed")
            self.connection.execute(
                """UPDATE session_connect_workflows SET stage='activation_requested',
                   updated_at=? WHERE workflow_id=?""",
                (_now(), workflow_id),
            )
        return self.get(workflow_id)

    def prepare_marker(self, workflow_id: str, lease_token: str | None) -> ConnectWorkflow:
        now = _now()
        with self.state._immediate_transaction():
            workflow = self.get(workflow_id)
            if workflow.stage != "activation_requested" or workflow.lease_token != lease_token:
                raise StateError("connect_worker_lease_changed")
            CodexSessionOrigins(self.state).preview(self._adoption_request(workflow))
            self.connection.execute(
                """UPDATE session_connect_workflows SET stage='marker_ready',lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE workflow_id=?""",
                (now, workflow_id),
            )
            self._insert_outbox_locked(
                workflow_id,
                "activation_marker",
                workflow.destination_chat_id or 0,
                workflow.destination_thread_id or 0,
                "Служебная граница подключения сессии Hub.",
            )
        return self.get(workflow_id)

    def fail_worker(self, workflow_id: str, lease_token: str | None, error_code: str) -> None:
        with self.state._immediate_transaction():
            workflow = self.get(workflow_id)
            if workflow.lease_token != lease_token:
                raise StateError("connect_worker_lease_changed")
            self.connection.execute(
                """UPDATE session_connect_workflows SET stage='failed',error_code=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE workflow_id=?""",
                (error_code[:128], _now(), workflow_id),
            )
            self._insert_outbox_locked(
                workflow_id,
                "notice",
                workflow.destination_chat_id or workflow.owner_user_id,
                workflow.destination_thread_id or 1,
                "Подключение остановлено безопасно. Текущая сессия не изменена; запустите /connect снова.",
            )

    def _insert_outbox_locked(
        self,
        workflow_id: str,
        kind: str,
        chat_id: int,
        thread_id: int,
        telegram_html: str,
        reply_markup: dict[str, object] | None = None,
    ) -> str:
        outbox_id = _token()
        now = _now()
        self.connection.execute(
            """INSERT INTO session_connect_outbox
               (outbox_id,workflow_id,kind,chat_id,thread_id,telegram_html,
                reply_markup_json,status,available_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,'prepared',?,?,?)""",
            (
                outbox_id,
                workflow_id,
                kind,
                chat_id,
                thread_id,
                telegram_html,
                None if reply_markup is None else json.dumps(reply_markup, ensure_ascii=False),
                now,
                now,
                now,
            ),
        )
        return outbox_id

    def recover_stale_outbox(self) -> None:
        now = _now()
        with self.state._immediate_transaction():
            rows = self.connection.execute(
                """SELECT outbox_id,workflow_id,kind FROM session_connect_outbox
                   WHERE status='leased' AND lease_expires_at<=?""",
                (now,),
            ).fetchall()
            for row in rows:
                if row["kind"] == "activation_marker":
                    self.connection.execute(
                        """UPDATE session_connect_outbox SET status='unknown',error_code='marker_outcome_unknown',
                           lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=?
                           WHERE outbox_id=?""",
                        (now, row["outbox_id"]),
                    )
                    self.connection.execute(
                        """UPDATE session_connect_workflows SET stage='marker_unknown',
                           error_code='marker_outcome_unknown',updated_at=? WHERE workflow_id=?
                           AND stage='marker_ready'""",
                        (now, row["workflow_id"]),
                    )
                else:
                    self.connection.execute(
                        """UPDATE session_connect_outbox SET status='prepared',lease_owner=NULL,
                           lease_token=NULL,lease_expires_at=NULL,updated_at=? WHERE outbox_id=?""",
                        (now, row["outbox_id"]),
                    )

    def lease_outbox(self, sender_id: str) -> ConnectOutbox | None:
        now = datetime.now(timezone.utc)
        token = _token()
        with self.state._immediate_transaction():
            row = self.connection.execute(
                """SELECT outbox_id FROM session_connect_outbox
                   WHERE status='prepared' AND available_at<=?
                   ORDER BY CASE kind WHEN 'activation_marker' THEN 0 ELSE 1 END, created_at LIMIT 1""",
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                return None
            cursor = self.connection.execute(
                """UPDATE session_connect_outbox SET status='leased',lease_owner=?,lease_token=?,
                   lease_expires_at=?,attempt_count=attempt_count+1,updated_at=?
                   WHERE outbox_id=? AND status='prepared'""",
                (
                    sender_id,
                    token,
                    (now + OUTBOX_LEASE).isoformat(),
                    now.isoformat(),
                    row["outbox_id"],
                ),
            )
            if cursor.rowcount != 1:
                return None
        current = self.connection.execute(
            "SELECT * FROM session_connect_outbox WHERE outbox_id=?", (row["outbox_id"],)
        ).fetchone()
        return self._outbox(current)

    def complete_marker(
        self, outbox: ConnectOutbox, *, telegram_message_id: int
    ) -> ConnectWorkflow:
        if telegram_message_id <= 0 or outbox.kind != "activation_marker":
            raise StateError("invalid_activation_marker")
        now = _now()
        with self.state._immediate_transaction():
            current = self.connection.execute(
                "SELECT * FROM session_connect_outbox WHERE outbox_id=?", (outbox.outbox_id,)
            ).fetchone()
            if (
                current is None
                or current["status"] != "leased"
                or current["lease_token"] != outbox.lease_token
            ):
                raise StateError("connect_outbox_lease_changed")
            workflow = self.get(outbox.workflow_id)
            if workflow.stage != "marker_ready":
                raise StateError("connect_marker_stale")
            origins = CodexSessionOrigins(self.state)
            attached = origins._attach_locked(
                self._adoption_request(workflow),
                expected_session_id=workflow.expected_session_id,
            )
            origins.activate(
                attached.session.session_id, telegram_message_id, attached.topic.topic_id
            )
            self.connection.execute(
                """UPDATE agent_sessions SET writer_mode='telegram',updated_at=?
                   WHERE session_id=?""",
                (now, attached.session.session_id),
            )
            self.connection.execute(
                """UPDATE session_connect_outbox SET status='delivered',telegram_message_id=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,delivered_at=?,updated_at=?
                   WHERE outbox_id=?""",
                (telegram_message_id, now, now, outbox.outbox_id),
            )
            self.connection.execute(
                """UPDATE session_connect_workflows SET stage='completed',result_session_id=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,error_code=NULL,updated_at=?
                   WHERE workflow_id=?""",
                (attached.session.session_id, now, workflow.workflow_id),
            )
            if workflow.code_id is not None:
                self.connection.execute(
                    """UPDATE session_connect_codes SET consumed_at=?,result_session_id=?
                       WHERE code_id=? AND consumed_at IS NULL""",
                    (now, attached.session.session_id, workflow.code_id),
                )
            replacement = (
                " Прежняя Hub-привязка архивирована; истории разговоров не объединялись."
                if workflow.replaces_session_id is not None
                else ""
            )
            self._insert_outbox_locked(
                workflow.workflow_id,
                "result",
                workflow.destination_chat_id or 0,
                workflow.destination_thread_id or 0,
                "Сессия подключена. Следующее обычное сообщение продолжит выбранный разговор."
                + replacement,
            )
        return self.get(outbox.workflow_id)

    def complete_outbox(self, outbox: ConnectOutbox, telegram_message_id: int) -> None:
        with self.state._immediate_transaction():
            cursor = self.connection.execute(
                """UPDATE session_connect_outbox SET status='delivered',telegram_message_id=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,delivered_at=?,updated_at=?
                   WHERE outbox_id=? AND status='leased' AND lease_token=?""",
                (
                    telegram_message_id,
                    _now(),
                    _now(),
                    outbox.outbox_id,
                    outbox.lease_token,
                ),
            )
            if cursor.rowcount != 1:
                raise StateError("connect_outbox_lease_changed")

    def retry_outbox(self, outbox: ConnectOutbox, error_code: str, delay: timedelta) -> None:
        with self.state._immediate_transaction():
            self.connection.execute(
                """UPDATE session_connect_outbox SET status='prepared',available_at=?,error_code=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE outbox_id=? AND status='leased' AND lease_token=?""",
                (
                    _deadline(delay),
                    error_code[:128],
                    _now(),
                    outbox.outbox_id,
                    outbox.lease_token,
                ),
            )

    def mark_marker_unknown(self, outbox: ConnectOutbox, error_code: str) -> None:
        with self.state._immediate_transaction():
            self.connection.execute(
                """UPDATE session_connect_outbox SET status='unknown',error_code=?,lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE outbox_id=? AND status='leased' AND lease_token=?""",
                (error_code[:128], _now(), outbox.outbox_id, outbox.lease_token),
            )
            self.connection.execute(
                """UPDATE session_connect_workflows SET stage='marker_unknown',error_code=?,
                   updated_at=? WHERE workflow_id=? AND stage='marker_ready'""",
                (error_code[:128], _now(), outbox.workflow_id),
            )

    def cancel(self, owner_user_id: int, workflow_id: str | None = None) -> bool:
        with self.state._immediate_transaction():
            if workflow_id is None:
                row = self.connection.execute(
                    """SELECT workflow_id FROM session_connect_workflows WHERE owner_user_id=?
                       AND stage NOT IN ('completed','cancelled','expired','failed','marker_unknown')
                       ORDER BY created_at DESC LIMIT 1""",
                    (owner_user_id,),
                ).fetchone()
                if row is None:
                    return False
                workflow_id = str(row["workflow_id"])
            cursor = self.connection.execute(
                """UPDATE session_connect_workflows SET stage='cancelled',updated_at=?
                   WHERE workflow_id=? AND owner_user_id=? AND stage NOT IN
                   ('completed','cancelled','expired','failed','marker_unknown')""",
                (_now(), workflow_id, owner_user_id),
            )
            return cursor.rowcount == 1

    @staticmethod
    def confirmation_text(workflow: ConnectWorkflow) -> str:
        source = html.escape(workflow.source_label or "Сохранённая сессия")
        destination = f"{workflow.destination_chat_id}:{workflow.destination_thread_id}"
        replacement = (
            " Текущая Codex-привязка будет архивирована."
            if workflow.replaces_session_id is not None
            else ""
        )
        return (
            f"Подключить <b>{source}</b> к теме <code>{destination}</code>? "
            "Закройте CLI. Истории не объединяются." + replacement
        )

    def confirmation_markup(self, workflow: ConnectWorkflow) -> dict[str, object]:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "CLI закрыт — подключить",
                        "callback_data": f"cx:ok:{workflow.workflow_id}",
                    }
                ],
                [{"text": "Отмена", "callback_data": f"cx:x:{workflow.workflow_id}"}],
            ]
        }

    @staticmethod
    def _adoption_request(workflow: ConnectWorkflow) -> AdoptionRequest:
        if (
            workflow.project_id is None
            or workflow.canonical_root is None
            or workflow.source_thread_id is None
            or workflow.destination_chat_id is None
            or workflow.destination_thread_id is None
        ):
            raise StateError("connect_workflow_incomplete")
        return AdoptionRequest(
            workflow.project_id,
            workflow.destination_chat_id,
            workflow.destination_thread_id,
            workflow.source_thread_id,
            workflow.canonical_root,
            workflow.model,
            workflow.effort,
            workflow.replaces_session_id,
        )


def code_digest(code: str) -> str:
    return hashlib.sha256(code.upper().encode("ascii", "strict")).hexdigest()
