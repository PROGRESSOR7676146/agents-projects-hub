"""Durable, model-free project and Telegram forum onboarding."""

from __future__ import annotations

import html
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .registry import PROJECT_ID
from .state import HubState, StateError, _now

if TYPE_CHECKING:
    from .hub_config import HubConfig

WORKFLOW_TTL = timedelta(minutes=30)
WORKER_LEASE = timedelta(minutes=2)
OUTBOX_LEASE = timedelta(seconds=30)
MAX_ROOTS = 12


def _token() -> str:
    return secrets.token_hex(8)


def _deadline(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) + delta).isoformat()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _safe_name(value: str) -> str:
    return " ".join(value.split())


@dataclass(frozen=True, slots=True)
class OnboardingOption:
    option_id: str
    workflow_id: str
    base_root: Path
    safe_label: str


@dataclass(frozen=True, slots=True)
class OnboardingWorkflow:
    workflow_id: str
    owner_user_id: int
    display_name: str | None
    project_id: str | None
    base_root: Path | None
    canonical_root: Path | None
    stage: str
    telegram_chat_id: int | None
    telegram_access_hash: int | None
    lease_token: str | None
    error_code: str | None
    expires_at: str


@dataclass(frozen=True, slots=True)
class OnboardingOutbox:
    outbox_id: str
    workflow_id: str
    chat_id: int
    telegram_html: str
    status: str
    lease_token: str | None
    attempt_count: int


@dataclass(frozen=True, slots=True)
class ProjectGroupBinding:
    project_id: str
    telegram_chat_id: int
    canonical_root: Path
    workflow_id: str


class ProjectOnboardingStore:
    """State transitions for the private Hub wizard and provisioning worker."""

    def __init__(self, state: HubState) -> None:
        self.state = state
        self.connection = state._connection

    @staticmethod
    def _workflow(row: sqlite3.Row) -> OnboardingWorkflow:
        return OnboardingWorkflow(
            workflow_id=str(row["workflow_id"]),
            owner_user_id=int(row["owner_user_id"]),
            display_name=None if row["display_name"] is None else str(row["display_name"]),
            project_id=None if row["project_id"] is None else str(row["project_id"]),
            base_root=None if row["base_root"] is None else Path(str(row["base_root"])),
            canonical_root=(
                None if row["canonical_root"] is None else Path(str(row["canonical_root"]))
            ),
            stage=str(row["stage"]),
            telegram_chat_id=(
                None if row["telegram_chat_id"] is None else int(row["telegram_chat_id"])
            ),
            telegram_access_hash=(
                None if row["telegram_access_hash"] is None else int(row["telegram_access_hash"])
            ),
            lease_token=None if row["lease_token"] is None else str(row["lease_token"]),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            expires_at=str(row["expires_at"]),
        )

    @staticmethod
    def _outbox(row: sqlite3.Row) -> OnboardingOutbox:
        return OnboardingOutbox(
            str(row["outbox_id"]),
            str(row["workflow_id"]),
            int(row["chat_id"]),
            str(row["telegram_html"]),
            str(row["status"]),
            None if row["lease_token"] is None else str(row["lease_token"]),
            int(row["attempt_count"]),
        )

    def get(self, workflow_id: str) -> OnboardingWorkflow:
        row = self.connection.execute(
            "SELECT * FROM project_onboarding_workflows WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise StateError("onboarding_workflow_unknown")
        return self._workflow(row)

    def active_for_owner(self, owner_user_id: int) -> OnboardingWorkflow | None:
        row = self.connection.execute(
            """SELECT * FROM project_onboarding_workflows WHERE owner_user_id=?
               AND stage NOT IN ('completed','cancelled','expired','failed')
               ORDER BY created_at DESC LIMIT 1""",
            (owner_user_id,),
        ).fetchone()
        if row is None:
            return None
        workflow = self._workflow(row)
        if _parse_time(workflow.expires_at) <= datetime.now(timezone.utc) and workflow.stage in {
            "awaiting_name",
            "choosing_root",
            "awaiting_folder",
            "confirming",
        }:
            with self.state._immediate_transaction():
                self.connection.execute(
                    """UPDATE project_onboarding_workflows
                       SET stage='expired',updated_at=? WHERE workflow_id=? AND stage=?""",
                    (_now(), workflow.workflow_id, workflow.stage),
                )
            return None
        return workflow

    def start(self, *, owner_user_id: int, allowed_roots: tuple[Path, ...]) -> OnboardingWorkflow:
        if owner_user_id <= 0 or not allowed_roots or len(allowed_roots) > MAX_ROOTS:
            raise StateError("onboarding_roots_unavailable")
        resolved: list[Path] = []
        for root in allowed_roots:
            candidate = root.expanduser().resolve(strict=True)
            if not candidate.is_dir() or candidate in resolved:
                raise StateError("onboarding_root_invalid")
            resolved.append(candidate)
        workflow_id = _token()
        now = _now()
        with self.state._immediate_transaction():
            self.connection.execute(
                """UPDATE project_onboarding_workflows SET stage='cancelled',updated_at=?
                   WHERE owner_user_id=? AND stage IN
                   ('awaiting_name','choosing_root','awaiting_folder','confirming')""",
                (now, owner_user_id),
            )
            self.connection.execute(
                """INSERT INTO project_onboarding_workflows
                   (workflow_id,owner_user_id,stage,expires_at,created_at,updated_at)
                   VALUES (?,?,'awaiting_name',?,?,?)""",
                (workflow_id, owner_user_id, _deadline(WORKFLOW_TTL), now, now),
            )
            for index, root in enumerate(resolved, start=1):
                label = root.name or f"Allowed root {index}"
                self.connection.execute(
                    """INSERT INTO project_onboarding_options
                       (option_id,workflow_id,base_root,safe_label,created_at)
                       VALUES (?,?,?,?,?)""",
                    (_token(), workflow_id, str(root), label[:160], now),
                )
        return self.get(workflow_id)

    def set_name(self, owner_user_id: int, workflow_id: str, value: str) -> OnboardingWorkflow:
        name = _safe_name(value)
        if not 1 <= len(name) <= 128 or not name.isprintable():
            raise StateError("onboarding_name_invalid")
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_onboarding_workflows
                   SET display_name=?,stage='choosing_root',updated_at=?
                   WHERE workflow_id=? AND owner_user_id=? AND stage='awaiting_name'""",
                (name, _now(), workflow_id, owner_user_id),
            ).rowcount
            if changed != 1:
                raise StateError("onboarding_selection_stale")
        return self.get(workflow_id)

    def options(self, workflow_id: str) -> tuple[OnboardingOption, ...]:
        rows = self.connection.execute(
            """SELECT * FROM project_onboarding_options WHERE workflow_id=?
               ORDER BY created_at,option_id""",
            (workflow_id,),
        ).fetchall()
        return tuple(
            OnboardingOption(
                str(row["option_id"]),
                str(row["workflow_id"]),
                Path(str(row["base_root"])),
                str(row["safe_label"]),
            )
            for row in rows
        )

    def roots_markup(self, workflow_id: str) -> dict[str, object]:
        rows = [
            [{"text": option.safe_label, "callback_data": f"po:r:{option.option_id}"}]
            for option in self.options(workflow_id)
        ]
        rows.append([{"text": "Отмена", "callback_data": f"po:x:{workflow_id}"}])
        return {"inline_keyboard": rows}

    def select_root(self, owner_user_id: int, option_id: str) -> OnboardingWorkflow:
        with self.state._immediate_transaction():
            row = self.connection.execute(
                """SELECT o.*,w.owner_user_id,w.stage FROM project_onboarding_options o
                   JOIN project_onboarding_workflows w ON w.workflow_id=o.workflow_id
                   WHERE o.option_id=?""",
                (option_id,),
            ).fetchone()
            if (
                row is None
                or int(row["owner_user_id"]) != owner_user_id
                or str(row["stage"]) != "choosing_root"
            ):
                raise StateError("onboarding_selection_stale")
            self.connection.execute(
                """UPDATE project_onboarding_workflows
                   SET base_root=?,stage='awaiting_folder',updated_at=? WHERE workflow_id=?""",
                (str(row["base_root"]), _now(), str(row["workflow_id"])),
            )
        return self.get(str(row["workflow_id"]))

    def set_folder(self, owner_user_id: int, workflow_id: str, folder: str) -> OnboardingWorkflow:
        project_id = folder.strip().lower()
        if PROJECT_ID.fullmatch(project_id) is None:
            raise StateError("onboarding_folder_invalid")
        workflow = self.get(workflow_id)
        if workflow.owner_user_id != owner_user_id or workflow.stage != "awaiting_folder":
            raise StateError("onboarding_selection_stale")
        assert workflow.base_root is not None
        canonical = (workflow.base_root / project_id).resolve(strict=False)
        try:
            relative = canonical.relative_to(workflow.base_root)
        except ValueError:
            raise StateError("onboarding_root_invalid") from None
        if len(relative.parts) != 1:
            raise StateError("onboarding_root_invalid")
        with self.state._immediate_transaction():
            if self.connection.execute(
                "SELECT 1 FROM project_group_bindings WHERE project_id=? OR canonical_root=?",
                (project_id, str(canonical)),
            ).fetchone():
                raise StateError("onboarding_project_exists")
            if self.connection.execute(
                """SELECT 1 FROM project_onboarding_workflows
                   WHERE workflow_id<>? AND (project_id=? OR canonical_root=?)
                   AND stage NOT IN ('completed','cancelled','expired','failed')""",
                (workflow_id, project_id, str(canonical)),
            ).fetchone():
                raise StateError("onboarding_project_exists")
            changed = self.connection.execute(
                """UPDATE project_onboarding_workflows SET project_id=?,canonical_root=?,
                   stage='confirming',updated_at=? WHERE workflow_id=? AND owner_user_id=?
                   AND stage='awaiting_folder'""",
                (project_id, str(canonical), _now(), workflow_id, owner_user_id),
            ).rowcount
            if changed != 1:
                raise StateError("onboarding_selection_stale")
        return self.get(workflow_id)

    def confirmation_text(self, workflow: OnboardingWorkflow) -> str:
        if workflow.display_name is None or workflow.canonical_root is None:
            raise StateError("onboarding_incomplete")
        return (
            "<b>Создать проектную группу?</b>\n"
            f"Группа: {html.escape(workflow.display_name)}\n"
            f"Проект: <code>{html.escape(workflow.project_id or '')}</code>\n"
            f"Каталог: <code>{html.escape(str(workflow.canonical_root))}</code>\n\n"
            "Hub создаст каталог и Git-репозиторий, если каталога ещё нет, затем создаст "
            "приватную группу с темами и добавит настроенных ботов."
        )

    def confirmation_markup(self, workflow_id: str) -> dict[str, object]:
        return {
            "inline_keyboard": [
                [{"text": "Создать", "callback_data": f"po:ok:{workflow_id}"}],
                [{"text": "Отмена", "callback_data": f"po:x:{workflow_id}"}],
            ]
        }

    def confirm(self, owner_user_id: int, workflow_id: str) -> OnboardingWorkflow:
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_onboarding_workflows SET stage='queued',updated_at=?
                   WHERE workflow_id=? AND owner_user_id=? AND stage='confirming'""",
                (_now(), workflow_id, owner_user_id),
            ).rowcount
            if changed != 1:
                workflow = self.get(workflow_id)
                if workflow.owner_user_id == owner_user_id and workflow.stage in {
                    "queued",
                    "preparing_root",
                    "creating_group",
                    "configuring_group",
                    "committing_binding",
                    "completed",
                }:
                    return workflow
                raise StateError("onboarding_selection_stale")
        return self.get(workflow_id)

    def cancel(self, owner_user_id: int, workflow_id: str | None = None) -> bool:
        with self.state._immediate_transaction():
            if workflow_id is None:
                row = self.connection.execute(
                    """SELECT workflow_id FROM project_onboarding_workflows
                       WHERE owner_user_id=? AND stage IN
                       ('awaiting_name','choosing_root','awaiting_folder','confirming')
                       ORDER BY created_at DESC LIMIT 1""",
                    (owner_user_id,),
                ).fetchone()
                if row is None:
                    return False
                workflow_id = str(row["workflow_id"])
            return (
                self.connection.execute(
                    """UPDATE project_onboarding_workflows SET stage='cancelled',updated_at=?
                       WHERE workflow_id=? AND owner_user_id=? AND stage IN
                       ('awaiting_name','choosing_root','awaiting_folder','confirming')""",
                    (_now(), workflow_id, owner_user_id),
                ).rowcount
                == 1
            )

    def claim_next(self, worker_id: str) -> OnboardingWorkflow | None:
        token = _token()
        now = _now()
        with self.state._immediate_transaction():
            stale = self.connection.execute(
                """SELECT workflow_id,owner_user_id,stage FROM project_onboarding_workflows
                   WHERE lease_expires_at IS NOT NULL AND lease_expires_at<=?
                   AND stage IN ('preparing_root','creating_group','configuring_group')""",
                (now,),
            ).fetchall()
            for item in stale:
                workflow_id = str(item["workflow_id"])
                stage = str(item["stage"])
                if stage == "preparing_root":
                    self.connection.execute(
                        """UPDATE project_onboarding_workflows SET stage='queued',
                           lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=?
                           WHERE workflow_id=? AND stage='preparing_root'""",
                        (now, workflow_id),
                    )
                    continue
                unknown = "group_unknown" if stage == "creating_group" else "configuration_unknown"
                self.connection.execute(
                    """UPDATE project_onboarding_workflows SET stage=?,error_code='worker_lost',
                       lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=?
                       WHERE workflow_id=? AND stage=?""",
                    (unknown, now, workflow_id, stage),
                )
                self._insert_outbox(
                    workflow_id,
                    int(item["owner_user_id"]),
                    (
                        "Provisioning worker остановился во время внешней операции. "
                        "Автоповтор отключён; проверьте созданную группу локально."
                    ),
                    now=now,
                )
            busy = self.connection.execute(
                """SELECT 1 FROM project_onboarding_workflows
                   WHERE lease_expires_at>? AND stage IN
                   ('preparing_root','creating_group','configuring_group','committing_binding')
                   LIMIT 1""",
                (now,),
            ).fetchone()
            if busy is not None:
                return None
            row = self.connection.execute(
                """SELECT workflow_id FROM project_onboarding_workflows
                   WHERE stage='queued' OR
                   (stage='committing_binding' AND lease_expires_at<=?)
                   ORDER BY created_at LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            workflow_id = str(row["workflow_id"])
            changed = self.connection.execute(
                """UPDATE project_onboarding_workflows SET
                   stage=CASE WHEN stage='queued' THEN 'preparing_root' ELSE stage END,
                   lease_owner=?,lease_token=?,lease_expires_at=?,updated_at=?
                   WHERE workflow_id=? AND (stage='queued' OR
                   (stage='committing_binding' AND lease_expires_at<=?))""",
                (worker_id, token, _deadline(WORKER_LEASE), now, workflow_id, now),
            ).rowcount
            if changed != 1:
                return None
        return self.get(workflow_id)

    def _leased_transition(
        self,
        workflow_id: str,
        lease_token: str,
        *,
        expected: str,
        stage: str,
        telegram_chat_id: int | None = None,
        telegram_access_hash: int | None = None,
    ) -> OnboardingWorkflow:
        fields = "stage=?,updated_at=?"
        values: list[object] = [stage, _now()]
        if telegram_chat_id is not None:
            fields += ",telegram_chat_id=?,telegram_access_hash=?"
            values.extend((telegram_chat_id, telegram_access_hash))
        values.extend((workflow_id, lease_token, expected))
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                f"""UPDATE project_onboarding_workflows SET {fields}
                    WHERE workflow_id=? AND lease_token=? AND stage=?""",
                tuple(values),
            ).rowcount
            if changed != 1:
                raise StateError("onboarding_worker_lease_lost")
        return self.get(workflow_id)

    def mark_root_ready(self, workflow_id: str, lease_token: str) -> OnboardingWorkflow:
        workflow = self.get(workflow_id)
        return self._leased_transition(
            workflow_id,
            lease_token,
            expected="preparing_root",
            stage="configuring_group"
            if workflow.telegram_chat_id is not None
            else "creating_group",
        )

    def reconcile_unknown(
        self,
        workflow_id: str,
        *,
        telegram_chat_id: int | None = None,
        telegram_access_hash: int | None = None,
        confirm: str,
    ) -> OnboardingWorkflow:
        workflow = self.get(workflow_id)
        if workflow.stage not in {"group_unknown", "configuration_unknown"}:
            raise StateError("onboarding_reconcile_not_unknown")
        chat_id = telegram_chat_id or workflow.telegram_chat_id
        access_hash = telegram_access_hash or workflow.telegram_access_hash
        if (
            chat_id is None
            or access_hash is None
            or not str(chat_id).startswith("-100")
            or confirm != f"{workflow_id}:{chat_id}"
        ):
            raise StateError("onboarding_reconcile_confirmation_invalid")
        with self.state._immediate_transaction():
            conflict = self.connection.execute(
                """SELECT 1 FROM project_group_bindings
                   WHERE telegram_chat_id=? OR project_id=?""",
                (chat_id, workflow.project_id),
            ).fetchone()
            if conflict is not None:
                raise StateError("onboarding_binding_conflict")
            changed = self.connection.execute(
                """UPDATE project_onboarding_workflows SET telegram_chat_id=?,
                   telegram_access_hash=?,stage='queued',error_code=NULL,updated_at=?
                   WHERE workflow_id=? AND stage IN ('group_unknown','configuration_unknown')""",
                (chat_id, access_hash, _now(), workflow_id),
            ).rowcount
            if changed != 1:
                raise StateError("onboarding_selection_stale")
        return self.get(workflow_id)

    def mark_group_created(
        self,
        workflow_id: str,
        lease_token: str,
        *,
        telegram_chat_id: int,
        telegram_access_hash: int,
    ) -> OnboardingWorkflow:
        if not str(telegram_chat_id).startswith("-100"):
            raise StateError("onboarding_chat_invalid")
        return self._leased_transition(
            workflow_id,
            lease_token,
            expected="creating_group",
            stage="configuring_group",
            telegram_chat_id=telegram_chat_id,
            telegram_access_hash=telegram_access_hash,
        )

    def mark_configured(self, workflow_id: str, lease_token: str) -> OnboardingWorkflow:
        return self._leased_transition(
            workflow_id,
            lease_token,
            expected="configuring_group",
            stage="committing_binding",
        )

    def _terminal_notice(
        self,
        workflow_id: str,
        lease_token: str,
        *,
        expected: str,
        stage: str,
        error_code: str,
        notice: str,
    ) -> OnboardingWorkflow:
        now = _now()
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_onboarding_workflows SET stage=?,error_code=?,updated_at=?
                   WHERE workflow_id=? AND lease_token=? AND stage=?""",
                (stage, error_code[:128], now, workflow_id, lease_token, expected),
            ).rowcount
            if changed != 1:
                raise StateError("onboarding_worker_lease_lost")
            row = self.connection.execute(
                "SELECT owner_user_id FROM project_onboarding_workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            assert row is not None
            self._insert_outbox(workflow_id, int(row["owner_user_id"]), notice, now=now)
        return self.get(workflow_id)

    def mark_group_unknown(
        self, workflow_id: str, lease_token: str, error_code: str
    ) -> OnboardingWorkflow:
        return self._terminal_notice(
            workflow_id,
            lease_token,
            expected="creating_group",
            stage="group_unknown",
            error_code=error_code,
            notice=(
                "Не удалось определить, создана ли Telegram-группа. Автоповтор остановлен, "
                "чтобы не создать дубликат. Проверьте список групп и выполните локальное "
                "восстановление provisioning workflow."
            ),
        )

    def mark_configuration_unknown(
        self, workflow_id: str, lease_token: str, error_code: str
    ) -> OnboardingWorkflow:
        return self._terminal_notice(
            workflow_id,
            lease_token,
            expected="configuring_group",
            stage="configuration_unknown",
            error_code=error_code,
            notice=(
                "Группа создана, но результат добавления ботов или назначения прав неизвестен. "
                "Hub не повторяет операцию вслепую; требуется локальная проверка workflow."
            ),
        )

    def fail(
        self, workflow_id: str, lease_token: str, *, expected: str, error_code: str
    ) -> OnboardingWorkflow:
        return self._terminal_notice(
            workflow_id,
            lease_token,
            expected=expected,
            stage="failed",
            error_code=error_code,
            notice=(
                "Создание проектной группы остановлено безопасно. Уже созданные каталог или "
                "группа сохранены; "
                f"причина: <code>{html.escape(error_code[:128])}</code>."
            ),
        )

    def complete(self, workflow_id: str, lease_token: str) -> OnboardingWorkflow:
        now = _now()
        with self.state._immediate_transaction():
            row = self.connection.execute(
                """SELECT * FROM project_onboarding_workflows
                   WHERE workflow_id=? AND lease_token=? AND stage='committing_binding'""",
                (workflow_id, lease_token),
            ).fetchone()
            if row is None:
                raise StateError("onboarding_worker_lease_lost")
            project_id = str(row["project_id"])
            chat_id = int(row["telegram_chat_id"])
            canonical_root = str(row["canonical_root"])
            existing = self.connection.execute(
                """SELECT * FROM project_group_bindings
                   WHERE project_id=? OR telegram_chat_id=? OR canonical_root=?""",
                (project_id, chat_id, canonical_root),
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    """INSERT INTO project_group_bindings
                       (project_id,telegram_chat_id,canonical_root,workflow_id,created_at)
                       VALUES (?,?,?,?,?)""",
                    (project_id, chat_id, canonical_root, workflow_id, now),
                )
            elif not (
                str(existing["project_id"]) == project_id
                and int(existing["telegram_chat_id"]) == chat_id
                and str(existing["canonical_root"]) == canonical_root
                and str(existing["workflow_id"]) == workflow_id
            ):
                raise StateError("onboarding_binding_conflict")
            self.connection.execute(
                """UPDATE project_onboarding_workflows SET stage='completed',
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE workflow_id=?""",
                (now, workflow_id),
            )
            self._insert_outbox(
                workflow_id,
                int(row["owner_user_id"]),
                (
                    "Проектная группа создана и подключена. Темы включены, Hub и остальные "
                    f"настроенные боты добавлены. Проект: <code>{html.escape(project_id)}</code>."
                ),
                now=now,
            )
        return self.get(workflow_id)

    def binding_for_chat(self, chat_id: int) -> ProjectGroupBinding | None:
        row = self.connection.execute(
            "SELECT * FROM project_group_bindings WHERE telegram_chat_id=?", (chat_id,)
        ).fetchone()
        if row is None:
            return None
        return ProjectGroupBinding(
            str(row["project_id"]),
            int(row["telegram_chat_id"]),
            Path(str(row["canonical_root"])),
            str(row["workflow_id"]),
        )

    def bindings(self) -> tuple[ProjectGroupBinding, ...]:
        rows = self.connection.execute(
            "SELECT * FROM project_group_bindings ORDER BY created_at,project_id"
        ).fetchall()
        return tuple(
            ProjectGroupBinding(
                str(row["project_id"]),
                int(row["telegram_chat_id"]),
                Path(str(row["canonical_root"])),
                str(row["workflow_id"]),
            )
            for row in rows
        )

    def _insert_outbox(self, workflow_id: str, chat_id: int, text: str, *, now: str) -> None:
        self.connection.execute(
            """INSERT INTO project_onboarding_outbox
               (outbox_id,workflow_id,chat_id,telegram_html,status,available_at,created_at,updated_at)
               VALUES (?,?,?,?, 'prepared',?,?,?)""",
            (_token(), workflow_id, chat_id, text, now, now, now),
        )

    def claim_outbox(self, sender_id: str) -> OnboardingOutbox | None:
        token = _token()
        now = _now()
        with self.state._immediate_transaction():
            self.connection.execute(
                """UPDATE project_onboarding_outbox SET status='unknown',
                   error_code='sender_lost',updated_at=?
                   WHERE status='leased' AND lease_expires_at<=?""",
                (now, now),
            )
            row = self.connection.execute(
                """SELECT outbox_id FROM project_onboarding_outbox
                   WHERE status='prepared' AND available_at<=?
                   ORDER BY created_at LIMIT 1""",
                (now,),
            ).fetchone()
            if row is None:
                return None
            changed = self.connection.execute(
                """UPDATE project_onboarding_outbox SET status='leased',lease_owner=?,
                   lease_token=?,lease_expires_at=?,attempt_count=attempt_count+1,updated_at=?
                   WHERE outbox_id=? AND status='prepared'""",
                (sender_id, token, _deadline(OUTBOX_LEASE), now, str(row["outbox_id"])),
            ).rowcount
            if changed != 1:
                return None
            result = self.connection.execute(
                "SELECT * FROM project_onboarding_outbox WHERE outbox_id=?",
                (str(row["outbox_id"]),),
            ).fetchone()
            assert result is not None
        return self._outbox(result)

    def mark_outbox_delivered(
        self, outbox_id: str, lease_token: str, telegram_message_id: int
    ) -> None:
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_onboarding_outbox SET status='delivered',
                   telegram_message_id=?,delivered_at=?,updated_at=?
                   WHERE outbox_id=? AND lease_token=? AND status='leased'""",
                (telegram_message_id, _now(), _now(), outbox_id, lease_token),
            ).rowcount
            if changed != 1:
                raise StateError("onboarding_outbox_lease_lost")

    def mark_outbox_unknown(self, outbox_id: str, lease_token: str, error_code: str) -> None:
        with self.state._immediate_transaction():
            self.connection.execute(
                """UPDATE project_onboarding_outbox SET status='unknown',error_code=?,updated_at=?
                   WHERE outbox_id=? AND lease_token=? AND status='leased'""",
                (error_code[:128], _now(), outbox_id, lease_token),
            )


def registered_project_ids(config: HubConfig, state: HubState) -> frozenset[str]:
    """Combine immutable static bindings with completed onboarding receipts."""
    values = {item.project_id for item in config.projects if item.telegram_chat_id is not None}
    values.update(item.project_id for item in ProjectOnboardingStore(state).bindings())
    return frozenset(values)


def project_id_for_chat(config: HubConfig, state: HubState, chat_id: int) -> str:
    try:
        return config.project_for_chat(chat_id).project_id
    except KeyError:
        binding = ProjectOnboardingStore(state).binding_for_chat(chat_id)
        if binding is None:
            raise
        return binding.project_id
