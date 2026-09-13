"""Durable, model-free project and Telegram forum onboarding."""

from __future__ import annotations

import html
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .registry import PROJECT_ID
from .state import HubState, StateError, _now

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
    required_owner_user_ids: tuple[int, ...]
    resume_stage: str | None
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


@dataclass(frozen=True, slots=True)
class ProjectCommandScope:
    telegram_chat_id: int
    bot_identity: str
    phase: str
    status: str
    lease_token: str | None
    attempt_count: int
    total_attempt_count: int


class ProjectOnboardingStore:
    """State transitions for the private Hub wizard and provisioning worker."""

    def __init__(self, state: HubState) -> None:
        self.state = state
        self.connection = state._connection

    @staticmethod
    def _workflow(row: sqlite3.Row) -> OnboardingWorkflow:
        raw_owner_ids = json.loads(str(row["required_owner_ids_json"]))
        if not isinstance(raw_owner_ids, list) or not all(
            isinstance(item, int) and not isinstance(item, bool) and item > 0
            for item in raw_owner_ids
        ):
            raise StateError("onboarding_owner_snapshot_invalid")
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
            required_owner_user_ids=tuple(raw_owner_ids),
            resume_stage=None if row["resume_stage"] is None else str(row["resume_stage"]),
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

    def latest_for_owner(self, owner_user_id: int) -> OnboardingWorkflow | None:
        row = self.connection.execute(
            """SELECT * FROM project_onboarding_workflows WHERE owner_user_id=?
               ORDER BY created_at DESC LIMIT 1""",
            (owner_user_id,),
        ).fetchone()
        return None if row is None else self._workflow(row)

    @staticmethod
    def status_text(workflow: OnboardingWorkflow) -> str:
        project = html.escape(workflow.project_id or "pending")
        if workflow.resume_stage is not None:
            state = "остановлен до локального resume"
        elif workflow.stage in {"group_unknown", "configuration_unknown"}:
            state = "требует локальной сверки"
        else:
            state = workflow.stage
        return f"Последний workflow: <code>{project}</code> · {html.escape(state)}"

    def active_for_owner(self, owner_user_id: int) -> OnboardingWorkflow | None:
        row = self.connection.execute(
            """SELECT * FROM project_onboarding_workflows WHERE owner_user_id=?
               AND stage NOT IN ('completed','cancelled','expired')
               AND (stage<>'failed' OR resume_stage IS NOT NULL)
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
        now = _now()
        stale = False
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_onboarding_workflows
                   SET display_name=?,stage='choosing_root',updated_at=?
                   WHERE workflow_id=? AND owner_user_id=? AND stage='awaiting_name'
                   AND expires_at>?""",
                (name, now, workflow_id, owner_user_id, now),
            ).rowcount
            if changed != 1:
                stale = True
                self.connection.execute(
                    """UPDATE project_onboarding_workflows SET stage='expired',updated_at=?
                       WHERE workflow_id=? AND owner_user_id=? AND stage='awaiting_name'
                       AND expires_at<=?""",
                    (now, workflow_id, owner_user_id, now),
                )
        if stale:
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
        now = _now()
        stale = False
        with self.state._immediate_transaction():
            row = self.connection.execute(
                """SELECT o.*,w.owner_user_id,w.stage,w.expires_at
                   FROM project_onboarding_options o
                   JOIN project_onboarding_workflows w ON w.workflow_id=o.workflow_id
                   WHERE o.option_id=?""",
                (option_id,),
            ).fetchone()
            if (
                row is None
                or int(row["owner_user_id"]) != owner_user_id
                or str(row["stage"]) != "choosing_root"
                or str(row["expires_at"]) <= now
            ):
                stale = True
                if row is not None and int(row["owner_user_id"]) == owner_user_id:
                    self.connection.execute(
                        """UPDATE project_onboarding_workflows SET stage='expired',updated_at=?
                           WHERE workflow_id=? AND stage='choosing_root' AND expires_at<=?""",
                        (now, str(row["workflow_id"]), now),
                    )
            else:
                self.connection.execute(
                    """UPDATE project_onboarding_workflows
                       SET base_root=?,stage='awaiting_folder',updated_at=? WHERE workflow_id=?""",
                    (str(row["base_root"]), now, str(row["workflow_id"])),
                )
        if stale or row is None:
            raise StateError("onboarding_selection_stale")
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
        now = _now()
        stale = False
        with self.state._immediate_transaction():
            if self.connection.execute(
                "SELECT 1 FROM project_group_bindings WHERE project_id=? OR canonical_root=?",
                (project_id, str(canonical)),
            ).fetchone():
                raise StateError("onboarding_project_exists")
            if self.connection.execute(
                """SELECT 1 FROM project_onboarding_workflows
                   WHERE workflow_id<>? AND (project_id=? OR canonical_root=?)
                   AND stage NOT IN ('completed','cancelled','expired')
                   AND (stage<>'failed' OR resume_stage IS NOT NULL)""",
                (workflow_id, project_id, str(canonical)),
            ).fetchone():
                raise StateError("onboarding_project_exists")
            changed = self.connection.execute(
                """UPDATE project_onboarding_workflows SET project_id=?,canonical_root=?,
                   stage='confirming',updated_at=? WHERE workflow_id=? AND owner_user_id=?
                   AND stage='awaiting_folder' AND expires_at>?""",
                (project_id, str(canonical), now, workflow_id, owner_user_id, now),
            ).rowcount
            if changed != 1:
                stale = True
                self.connection.execute(
                    """UPDATE project_onboarding_workflows SET stage='expired',updated_at=?
                       WHERE workflow_id=? AND owner_user_id=? AND stage='awaiting_folder'
                       AND expires_at<=?""",
                    (now, workflow_id, owner_user_id, now),
                )
        if stale:
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

    def confirm(
        self,
        owner_user_id: int,
        workflow_id: str,
        *,
        required_owner_user_ids: tuple[int, ...] | None = None,
    ) -> OnboardingWorkflow:
        required = required_owner_user_ids or (owner_user_id,)
        if (
            owner_user_id not in required
            or len(required) > 16
            or len(set(required)) != len(required)
            or any(isinstance(item, bool) or item <= 0 for item in required)
        ):
            raise StateError("onboarding_owner_snapshot_invalid")
        owner_snapshot = json.dumps(sorted(required), separators=(",", ":"))
        now = _now()
        stale = False
        repeated: OnboardingWorkflow | None = None
        with self.state._immediate_transaction():
            current = self.connection.execute(
                """SELECT project_id,canonical_root,stage FROM project_onboarding_workflows
                   WHERE workflow_id=? AND owner_user_id=?""",
                (workflow_id, owner_user_id),
            ).fetchone()
            if current is None:
                raise StateError("onboarding_selection_stale")
            if str(current["stage"]) == "confirming":
                binding_conflict = self.connection.execute(
                    """SELECT 1 FROM project_group_bindings
                       WHERE project_id=? OR canonical_root=?""",
                    (current["project_id"], current["canonical_root"]),
                ).fetchone()
                workflow_conflict = self.connection.execute(
                    """SELECT 1 FROM project_onboarding_workflows
                       WHERE workflow_id<>? AND (project_id=? OR canonical_root=?)
                       AND stage NOT IN ('completed','cancelled','expired')
                       AND (stage<>'failed' OR resume_stage IS NOT NULL)""",
                    (workflow_id, current["project_id"], current["canonical_root"]),
                ).fetchone()
                if binding_conflict is not None or workflow_conflict is not None:
                    raise StateError("onboarding_project_exists")
            changed = self.connection.execute(
                """UPDATE project_onboarding_workflows SET stage='queued',
                   required_owner_ids_json=?,updated_at=?
                   WHERE workflow_id=? AND owner_user_id=? AND stage='confirming'
                   AND expires_at>?""",
                (owner_snapshot, now, workflow_id, owner_user_id, now),
            ).rowcount
            if changed != 1:
                workflow = self.get(workflow_id)
                if workflow.stage == "confirming" and _parse_time(
                    workflow.expires_at
                ) <= datetime.now(timezone.utc):
                    self.connection.execute(
                        """UPDATE project_onboarding_workflows SET stage='expired',updated_at=?
                           WHERE workflow_id=? AND owner_user_id=? AND stage='confirming'""",
                        (now, workflow_id, owner_user_id),
                    )
                    stale = True
                elif workflow.owner_user_id == owner_user_id and workflow.stage in {
                    "queued",
                    "preparing_root",
                    "creating_group",
                    "configuring_group",
                    "committing_binding",
                    "completed",
                }:
                    repeated = workflow
                else:
                    stale = True
        if stale:
            raise StateError("onboarding_selection_stale")
        if repeated is not None:
            return repeated
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
        now = _now()
        fields = "stage=?,updated_at=?"
        values: list[object] = [stage, now]
        if telegram_chat_id is not None:
            fields += ",telegram_chat_id=?,telegram_access_hash=?"
            values.extend((telegram_chat_id, telegram_access_hash))
        values.extend((workflow_id, lease_token, expected, now))
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                f"""UPDATE project_onboarding_workflows SET {fields}
                    WHERE workflow_id=? AND lease_token=? AND stage=?
                    AND lease_expires_at>?""",
                tuple(values),
            ).rowcount
            if changed != 1:
                raise StateError("onboarding_worker_lease_lost")
        return self.get(workflow_id)

    def heartbeat_lease(
        self, workflow_id: str, lease_token: str, *, lease_seconds: int = 120
    ) -> OnboardingWorkflow:
        if not 30 <= lease_seconds <= 300:
            raise StateError("onboarding_lease_duration_invalid")
        now = datetime.now(timezone.utc)
        current = now.isoformat()
        deadline = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_onboarding_workflows SET lease_expires_at=?,updated_at=?
                   WHERE workflow_id=? AND lease_token=? AND lease_expires_at>?
                   AND stage IN ('preparing_root','creating_group','configuring_group',
                                 'committing_binding')""",
                (deadline, current, workflow_id, lease_token, current),
            ).rowcount
            if changed != 1:
                raise StateError("onboarding_worker_lease_lost")
        return self.get(workflow_id)

    def assert_lease(self, workflow_id: str, lease_token: str, *, expected: str) -> None:
        now = _now()
        row = self.connection.execute(
            """SELECT 1 FROM project_onboarding_workflows
               WHERE workflow_id=? AND lease_token=? AND stage=? AND lease_expires_at>?""",
            (workflow_id, lease_token, expected, now),
        ).fetchone()
        if row is None:
            raise StateError("onboarding_worker_lease_lost")

    def release_before_external(
        self, workflow_id: str, lease_token: str, *, expected: str
    ) -> OnboardingWorkflow:
        if expected not in {"creating_group", "configuring_group"}:
            raise StateError("onboarding_worker_release_invalid")
        now = _now()
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_onboarding_workflows SET stage='queued',lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE workflow_id=? AND lease_token=? AND stage=? AND lease_expires_at>?""",
                (now, workflow_id, lease_token, expected, now),
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
        required_owner_user_ids: tuple[int, ...],
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
        if (
            workflow.owner_user_id not in required_owner_user_ids
            or len(required_owner_user_ids) > 16
            or len(set(required_owner_user_ids)) != len(required_owner_user_ids)
            or any(isinstance(item, bool) or item <= 0 for item in required_owner_user_ids)
        ):
            raise StateError("onboarding_owner_snapshot_invalid")
        owner_snapshot = json.dumps(sorted(required_owner_user_ids), separators=(",", ":"))
        with self.state._immediate_transaction():
            conflict = self.connection.execute(
                """SELECT 1 FROM project_group_bindings
                   WHERE telegram_chat_id=? OR project_id=? OR canonical_root=?""",
                (chat_id, workflow.project_id, str(workflow.canonical_root)),
            ).fetchone()
            workflow_conflict = self.connection.execute(
                """SELECT 1 FROM project_onboarding_workflows
                   WHERE workflow_id<>? AND (project_id=? OR canonical_root=?)
                   AND stage NOT IN ('completed','cancelled','expired')
                   AND (stage<>'failed' OR resume_stage IS NOT NULL)""",
                (workflow_id, workflow.project_id, str(workflow.canonical_root)),
            ).fetchone()
            if conflict is not None or workflow_conflict is not None:
                raise StateError("onboarding_binding_conflict")
            changed = self.connection.execute(
                """UPDATE project_onboarding_workflows SET telegram_chat_id=?,
                   telegram_access_hash=?,stage='queued',error_code=NULL,
                   required_owner_ids_json=?,updated_at=?
                   WHERE workflow_id=? AND stage IN ('group_unknown','configuration_unknown')""",
                (chat_id, access_hash, owner_snapshot, _now(), workflow_id),
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
                """UPDATE project_onboarding_workflows SET stage=?,error_code=?,updated_at=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL
                   WHERE workflow_id=? AND lease_token=? AND stage=? AND lease_expires_at>?""",
                (stage, error_code[:128], now, workflow_id, lease_token, expected, now),
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

    def block(
        self,
        workflow_id: str,
        lease_token: str,
        *,
        expected: str,
        resume_stage: str,
        error_code: str,
        notice: str,
    ) -> OnboardingWorkflow:
        if resume_stage not in {"preparing_root", "configuring_group"}:
            raise StateError("onboarding_resume_stage_invalid")
        now = _now()
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_onboarding_workflows SET stage='failed',resume_stage=?,
                   error_code=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE workflow_id=? AND lease_token=? AND stage=? AND lease_expires_at>?""",
                (
                    resume_stage,
                    error_code[:128],
                    now,
                    workflow_id,
                    lease_token,
                    expected,
                    now,
                ),
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

    def resume_blocked(
        self,
        workflow_id: str,
        *,
        required_owner_user_ids: tuple[int, ...],
        confirm: str,
    ) -> OnboardingWorkflow:
        if confirm != workflow_id:
            raise StateError("onboarding_resume_confirmation_invalid")
        workflow = self.get(workflow_id)
        if (
            workflow.owner_user_id not in required_owner_user_ids
            or len(required_owner_user_ids) > 16
            or len(set(required_owner_user_ids)) != len(required_owner_user_ids)
            or any(isinstance(item, bool) or item <= 0 for item in required_owner_user_ids)
        ):
            raise StateError("onboarding_owner_snapshot_invalid")
        owner_snapshot = json.dumps(sorted(required_owner_user_ids), separators=(",", ":"))
        with self.state._immediate_transaction():
            binding_conflict = self.connection.execute(
                """SELECT 1 FROM project_group_bindings
                   WHERE project_id=? OR canonical_root=?""",
                (workflow.project_id, str(workflow.canonical_root)),
            ).fetchone()
            workflow_conflict = self.connection.execute(
                """SELECT 1 FROM project_onboarding_workflows
                   WHERE workflow_id<>? AND (project_id=? OR canonical_root=?)
                   AND stage NOT IN ('completed','cancelled','expired')
                   AND (stage<>'failed' OR resume_stage IS NOT NULL)""",
                (workflow_id, workflow.project_id, str(workflow.canonical_root)),
            ).fetchone()
            if binding_conflict is not None or workflow_conflict is not None:
                raise StateError("onboarding_project_exists")
            changed = self.connection.execute(
                """UPDATE project_onboarding_workflows SET stage='queued',resume_stage=NULL,
                   error_code=NULL,required_owner_ids_json=?,updated_at=?
                   WHERE workflow_id=? AND stage='failed'
                   AND resume_stage IS NOT NULL""",
                (owner_snapshot, _now(), workflow_id),
            ).rowcount
            if changed != 1:
                raise StateError("onboarding_resume_not_blocked")
        return self.get(workflow_id)

    def assert_reservation(self, workflow_id: str) -> None:
        workflow = self.get(workflow_id)
        if workflow.project_id is None or workflow.canonical_root is None:
            raise StateError("onboarding_incomplete")
        binding_conflict = self.connection.execute(
            """SELECT 1 FROM project_group_bindings
               WHERE project_id=? OR canonical_root=?""",
            (workflow.project_id, str(workflow.canonical_root)),
        ).fetchone()
        workflow_conflict = self.connection.execute(
            """SELECT 1 FROM project_onboarding_workflows
               WHERE workflow_id<>? AND (project_id=? OR canonical_root=?)
               AND stage NOT IN ('completed','cancelled','expired')
               AND (stage<>'failed' OR resume_stage IS NOT NULL)""",
            (workflow_id, workflow.project_id, str(workflow.canonical_root)),
        ).fetchone()
        if binding_conflict is not None or workflow_conflict is not None:
            raise StateError("onboarding_project_exists")

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
                   WHERE workflow_id=? AND lease_token=? AND stage='committing_binding'
                   AND lease_expires_at>?""",
                (workflow_id, lease_token, now),
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

    def retry_outbox(
        self, outbox_id: str, lease_token: str, error_code: str, *, delay_seconds: int
    ) -> None:
        if not 0 <= delay_seconds <= 86_400:
            raise StateError("onboarding_outbox_retry_invalid")
        now = datetime.now(timezone.utc)
        available = (now + timedelta(seconds=delay_seconds)).isoformat()
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_onboarding_outbox SET
                   status=CASE WHEN attempt_count>=20 THEN 'failed' ELSE 'prepared' END,
                   available_at=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                   error_code=?,updated_at=?
                   WHERE outbox_id=? AND lease_token=? AND status='leased'""",
                (available, error_code[:128], now.isoformat(), outbox_id, lease_token),
            ).rowcount
            if changed != 1:
                raise StateError("onboarding_outbox_lease_lost")

    def fail_outbox(self, outbox_id: str, lease_token: str, error_code: str) -> None:
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_onboarding_outbox SET status='failed',error_code=?,updated_at=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL
                   WHERE outbox_id=? AND lease_token=? AND status='leased'""",
                (error_code[:128], _now(), outbox_id, lease_token),
            ).rowcount
            if changed != 1:
                raise StateError("onboarding_outbox_lease_lost")

    def mark_outbox_unknown(self, outbox_id: str, lease_token: str, error_code: str) -> None:
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_onboarding_outbox SET status='unknown',error_code=?,updated_at=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL
                   WHERE outbox_id=? AND lease_token=? AND status='leased'""",
                (error_code[:128], _now(), outbox_id, lease_token),
            ).rowcount
            if changed != 1:
                raise StateError("onboarding_outbox_lease_lost")

    def ensure_command_scope_tasks(self, bot_identities: tuple[str, ...]) -> None:
        if (
            not bot_identities
            or len(set(bot_identities)) != len(bot_identities)
            or any(not identity or len(identity) > 64 for identity in bot_identities)
        ):
            raise StateError("project_command_scope_identities_invalid")
        now = _now()
        with self.state._immediate_transaction():
            for identity in bot_identities:
                self.connection.execute(
                    """INSERT OR IGNORE INTO project_command_scopes
                       (telegram_chat_id,bot_identity,phase,status,available_at,updated_at)
                       SELECT telegram_chat_id,?,'set','pending',
                              COALESCE((SELECT available_at FROM project_command_cooldowns
                                        WHERE bot_identity=?),?),?
                       FROM project_group_bindings""",
                    (identity, identity, now, now),
                )

    def claim_command_scope(
        self, sender_id: str, bot_identities: tuple[str, ...]
    ) -> ProjectCommandScope | None:
        if not bot_identities:
            return None
        token = _token()
        now = _now()
        placeholders = ",".join("?" for _ in bot_identities)
        with self.state._immediate_transaction():
            self.connection.execute(
                """UPDATE project_command_scopes SET
                   status=CASE WHEN attempt_count>=20 OR total_attempt_count>=40
                               THEN 'failed' ELSE 'pending' END,
                   lease_owner=NULL,
                   lease_token=NULL,lease_expires_at=NULL,error_code='sender_lost',updated_at=?
                   WHERE status='leased' AND lease_expires_at<=?""",
                (now, now),
            )
            row = self.connection.execute(
                f"""SELECT telegram_chat_id,bot_identity FROM project_command_scopes
                    WHERE status='pending' AND attempt_count<20
                    AND total_attempt_count<40 AND available_at<=?
                    AND bot_identity IN ({placeholders})
                    AND NOT EXISTS (
                        SELECT 1 FROM project_command_cooldowns cooldown
                        WHERE cooldown.bot_identity=project_command_scopes.bot_identity
                        AND cooldown.available_at>?
                    )
                    ORDER BY available_at,telegram_chat_id,bot_identity LIMIT 1""",
                (now, *bot_identities, now),
            ).fetchone()
            if row is None:
                return None
            chat_id = int(row["telegram_chat_id"])
            bot_identity = str(row["bot_identity"])
            changed = self.connection.execute(
                """UPDATE project_command_scopes SET status='leased',lease_owner=?,lease_token=?,
                   lease_expires_at=?,attempt_count=attempt_count+1,
                   total_attempt_count=total_attempt_count+1,updated_at=?
                   WHERE telegram_chat_id=? AND bot_identity=? AND status='pending'
                   AND attempt_count<20 AND total_attempt_count<40""",
                (sender_id, token, _deadline(OUTBOX_LEASE), now, chat_id, bot_identity),
            ).rowcount
            if changed != 1:
                return None
            task = self.connection.execute(
                """SELECT * FROM project_command_scopes
                   WHERE telegram_chat_id=? AND bot_identity=?""",
                (chat_id, bot_identity),
            ).fetchone()
            assert task is not None
            return ProjectCommandScope(
                chat_id,
                bot_identity,
                str(task["phase"]),
                str(task["status"]),
                str(task["lease_token"]),
                int(task["attempt_count"]),
                int(task["total_attempt_count"]),
            )

    def complete_command_scope(self, task: ProjectCommandScope) -> None:
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_command_scopes SET status='ready',ready_at=?,updated_at=?,
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,error_code=NULL
                   WHERE telegram_chat_id=? AND bot_identity=?
                   AND status='leased' AND lease_token=? AND phase='verify'""",
                (_now(), _now(), task.telegram_chat_id, task.bot_identity, task.lease_token),
            ).rowcount
            if changed != 1:
                raise StateError("project_command_scope_lease_lost")

    def advance_command_scope_to_verify(self, task: ProjectCommandScope) -> None:
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_command_scopes SET phase='verify',
                   status=CASE WHEN total_attempt_count>=40 THEN 'failed' ELSE 'pending' END,
                   attempt_count=0,available_at=?,updated_at=?,lease_owner=NULL,lease_token=NULL,
                   lease_expires_at=NULL,
                   error_code=CASE WHEN total_attempt_count>=40
                                   THEN 'command_verify_budget_exhausted' ELSE NULL END
                   WHERE telegram_chat_id=? AND bot_identity=? AND phase='set'
                   AND status='leased' AND lease_token=?""",
                (_now(), _now(), task.telegram_chat_id, task.bot_identity, task.lease_token),
            ).rowcount
            if changed != 1:
                raise StateError("project_command_scope_lease_lost")

    def retry_command_scope(
        self,
        task: ProjectCommandScope,
        error_code: str,
        *,
        delay_seconds: int,
        restart_set: bool = False,
        defer_identity: bool = False,
    ) -> None:
        if not 0 <= delay_seconds <= 86_400:
            raise StateError("project_command_scope_retry_invalid")
        now = datetime.now(timezone.utc)
        available = (now + timedelta(seconds=delay_seconds)).isoformat()
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_command_scopes SET
                   status=CASE WHEN attempt_count>=20 OR total_attempt_count>=40
                               THEN 'failed' ELSE 'pending' END,
                   phase=CASE WHEN ? THEN 'set' ELSE phase END,
                   available_at=?,error_code=?,updated_at=?,lease_owner=NULL,lease_token=NULL,
                   lease_expires_at=NULL WHERE telegram_chat_id=? AND bot_identity=?
                   AND status='leased'
                   AND lease_token=?""",
                (
                    int(restart_set),
                    available,
                    error_code[:128],
                    now.isoformat(),
                    task.telegram_chat_id,
                    task.bot_identity,
                    task.lease_token,
                ),
            ).rowcount
            if changed != 1:
                raise StateError("project_command_scope_lease_lost")
            if defer_identity:
                self.connection.execute(
                    """INSERT INTO project_command_cooldowns
                       (bot_identity,available_at,updated_at) VALUES (?,?,?)
                       ON CONFLICT(bot_identity) DO UPDATE SET
                       available_at=MAX(available_at,excluded.available_at),updated_at=excluded.updated_at""",
                    (task.bot_identity, available, now.isoformat()),
                )
                self.connection.execute(
                    """UPDATE project_command_scopes SET available_at=?,updated_at=?
                       WHERE bot_identity=? AND status='pending' AND available_at<?""",
                    (available, now.isoformat(), task.bot_identity, available),
                )

    def release_command_scope(self, task: ProjectCommandScope) -> None:
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_command_scopes SET status='pending',
                   attempt_count=MAX(0,attempt_count-1),
                   total_attempt_count=MAX(0,total_attempt_count-1),
                   lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,updated_at=?
                   WHERE telegram_chat_id=? AND bot_identity=? AND status='leased' AND lease_token=?""",
                (_now(), task.telegram_chat_id, task.bot_identity, task.lease_token),
            ).rowcount
            if changed != 1:
                raise StateError("project_command_scope_lease_lost")

    def reset_failed_command_scope(self, telegram_chat_id: int, bot_identity: str) -> None:
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_command_scopes SET phase='set',status='pending',attempt_count=0,
                   total_attempt_count=0,
                   available_at=?,lease_owner=NULL,lease_token=NULL,lease_expires_at=NULL,
                   error_code=NULL,updated_at=?,ready_at=NULL
                   WHERE telegram_chat_id=? AND bot_identity=? AND status='failed'""",
                (_now(), _now(), telegram_chat_id, bot_identity),
            ).rowcount
            if changed != 1:
                raise StateError("project_command_scope_not_failed")
