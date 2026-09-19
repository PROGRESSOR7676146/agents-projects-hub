"""Owner-only durable editing of an existing project registration."""

from __future__ import annotations

import copy
import html
import secrets
import sqlite3
import subprocess
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .models import ProjectRegistry
from .project_admin import (
    _atomic_write,
    _read,
    prepare_project_root,
    registry_lock,
)
from .registry import RegistryError, load_registry
from .state import HubState, StateError, _now

WORKFLOW_TTL = timedelta(minutes=30)
MAX_PROJECTS = 100
MAX_ROOT_OPTIONS = 24


def _token() -> str:
    return secrets.token_hex(8)


def _deadline() -> str:
    return (datetime.now(timezone.utc) + WORKFLOW_TTL).isoformat()


def _safe_name(value: str) -> str:
    return " ".join(value.split())


def _safe_option_label(value: str, *, fallback: str, max_length: int = 160) -> str:
    visible = "".join(
        character
        for character in value
        if character.isprintable() and not unicodedata.category(character).startswith("C")
    )
    return " ".join(visible.split())[:max_length] or fallback[:max_length]


def _project_option_label(display_name: str, project_id: str) -> str:
    suffix = f" [{project_id}]"
    display = _safe_option_label(
        display_name,
        fallback="Project",
        max_length=160 - len(suffix),
    )
    return f"{display}{suffix}"


def _exact_git_root(path: Path) -> bool:
    try:
        result = subprocess.run(
            ("git", "-C", str(path), "rev-parse", "--show-toplevel"),
            check=True,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
        return Path(result.stdout.strip()).resolve(strict=True) == path.resolve(strict=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def validate_relocation_target(
    registry: ProjectRegistry,
    *,
    project_id: str,
    current_root: Path,
    target: Path,
    allow_create: bool,
) -> Path:
    """Validate one locally selected candidate; Telegram never supplies ``target``."""
    current = current_root.expanduser().resolve(strict=True)
    raw = target.expanduser()
    if not raw.is_absolute():
        raise RegistryError("project relocation target must be absolute")
    if raw.is_symlink():
        raise RegistryError("project relocation target cannot be a symlink")
    try:
        parent = raw.parent.resolve(strict=True)
    except OSError as exc:
        raise RegistryError("project relocation parent is unavailable") from exc
    allowed = tuple(root.resolve(strict=True) for root in registry.allowed_roots)
    if parent not in allowed or raw.name in {"", ".", ".."}:
        raise RegistryError("project relocation target must be a direct child of allowed_roots")
    canonical = raw.resolve(strict=False)
    if canonical.parent != parent:
        raise RegistryError("project relocation target escapes allowed_roots")
    if canonical == current:
        raise RegistryError("project relocation target is already active")
    if any(item.project_id != project_id and item.root == canonical for item in registry.projects):
        raise RegistryError("project relocation target is already registered")
    if not canonical.exists():
        if not allow_create or canonical.name != project_id:
            raise RegistryError("project relocation target must be prepared locally")
        return canonical
    if canonical.is_symlink() or not canonical.is_dir():
        raise RegistryError("project relocation target is not a safe directory")
    if _exact_git_root(canonical):
        return canonical.resolve(strict=True)
    if any(canonical.iterdir()):
        raise RegistryError("existing non-empty directory is not a Git root")
    if not allow_create or canonical.name != project_id:
        raise RegistryError("empty relocation target must use immutable project_id")
    return canonical.resolve(strict=True)


def _after_registry_write() -> None:
    """Crash boundary used by fault-injection tests."""


@dataclass(frozen=True, slots=True)
class ProjectEditWorkflow:
    workflow_id: str
    owner_user_id: int
    project_id: str | None
    old_display_name: str | None
    new_display_name: str | None
    old_root: Path | None
    old_binding_root: Path | None
    new_root: Path | None
    operation: str | None
    root_mode: str | None
    stage: str
    error_code: str | None
    expires_at: str


@dataclass(frozen=True, slots=True)
class ProjectEditProjectOption:
    option_id: str
    workflow_id: str
    project_id: str
    safe_label: str


@dataclass(frozen=True, slots=True)
class ProjectEditRootOption:
    option_id: str
    workflow_id: str
    root: Path
    root_mode: str
    safe_label: str


class ProjectEditStore:
    def __init__(self, state: HubState, registry_path: Path) -> None:
        self.state = state
        self.connection = state._connection
        self.registry_path = registry_path

    @staticmethod
    def _workflow(row: sqlite3.Row) -> ProjectEditWorkflow:
        return ProjectEditWorkflow(
            str(row["workflow_id"]),
            int(row["owner_user_id"]),
            None if row["project_id"] is None else str(row["project_id"]),
            None if row["old_display_name"] is None else str(row["old_display_name"]),
            None if row["new_display_name"] is None else str(row["new_display_name"]),
            None if row["old_root"] is None else Path(str(row["old_root"])),
            None if row["old_binding_root"] is None else Path(str(row["old_binding_root"])),
            None if row["new_root"] is None else Path(str(row["new_root"])),
            None if row["operation"] is None else str(row["operation"]),
            None if row["root_mode"] is None else str(row["root_mode"]),
            str(row["stage"]),
            None if row["error_code"] is None else str(row["error_code"]),
            str(row["expires_at"]),
        )

    def get(self, workflow_id: str) -> ProjectEditWorkflow:
        row = self.connection.execute(
            "SELECT * FROM project_edit_workflows WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
        if row is None:
            raise StateError("project_edit_workflow_unknown")
        return self._workflow(row)

    def active_for_owner(self, owner_user_id: int) -> ProjectEditWorkflow | None:
        row = self.connection.execute(
            """SELECT * FROM project_edit_workflows WHERE owner_user_id=?
               AND stage NOT IN ('completed','cancelled','expired','failed')
               ORDER BY created_at DESC LIMIT 1""",
            (owner_user_id,),
        ).fetchone()
        if row is None:
            return None
        workflow = self._workflow(row)
        if workflow.stage != "applying" and workflow.expires_at <= _now():
            with self.state._immediate_transaction():
                self.connection.execute(
                    """UPDATE project_edit_workflows SET stage='expired',updated_at=?
                       WHERE workflow_id=? AND stage=?""",
                    (_now(), workflow.workflow_id, workflow.stage),
                )
            return None
        return workflow

    def start(self, *, owner_user_id: int, project_ids: tuple[str, ...]) -> ProjectEditWorkflow:
        registry = load_registry(self.registry_path)
        selected = [
            project
            for project in registry.projects
            if project.enabled and project.project_id in project_ids
        ]
        if owner_user_id <= 0 or not selected or len(selected) > MAX_PROJECTS:
            raise StateError("project_edit_projects_unavailable")
        workflow_id = _token()
        now = _now()
        with self.state._immediate_transaction():
            self.connection.execute(
                """UPDATE project_edit_workflows SET stage='cancelled',updated_at=?
                   WHERE owner_user_id=? AND stage IN
                   ('choosing_project','choosing_operation','awaiting_name','choosing_root',
                    'confirming')""",
                (now, owner_user_id),
            )
            self.connection.execute(
                """INSERT INTO project_edit_workflows
                   (workflow_id,owner_user_id,stage,expires_at,created_at,updated_at)
                   VALUES (?,?,'choosing_project',?,?,?)""",
                (workflow_id, owner_user_id, _deadline(), now, now),
            )
            for project in selected:
                self.connection.execute(
                    """INSERT INTO project_edit_project_options
                       (option_id,workflow_id,project_id,safe_label,created_at)
                       VALUES (?,?,?,?,?)""",
                    (
                        _token(),
                        workflow_id,
                        project.project_id,
                        _project_option_label(project.display_name, project.project_id),
                        now,
                    ),
                )
        return self.get(workflow_id)

    def project_options(self, workflow_id: str) -> tuple[ProjectEditProjectOption, ...]:
        rows = self.connection.execute(
            """SELECT * FROM project_edit_project_options WHERE workflow_id=?
               ORDER BY created_at,option_id""",
            (workflow_id,),
        ).fetchall()
        return tuple(
            ProjectEditProjectOption(
                str(row["option_id"]),
                str(row["workflow_id"]),
                str(row["project_id"]),
                str(row["safe_label"]),
            )
            for row in rows
        )

    def project_markup(self, workflow_id: str) -> dict[str, object]:
        rows = [
            [{"text": option.safe_label, "callback_data": f"pe:p:{option.option_id}"}]
            for option in self.project_options(workflow_id)
        ]
        rows.append([{"text": "Отмена", "callback_data": f"pe:x:{workflow_id}"}])
        return {"inline_keyboard": rows}

    def select_project(self, owner_user_id: int, option_id: str) -> ProjectEditWorkflow:
        registry = load_registry(self.registry_path)
        now = _now()
        with self.state._immediate_transaction():
            row = self.connection.execute(
                """SELECT o.*,w.owner_user_id,w.stage,w.expires_at
                   FROM project_edit_project_options o
                   JOIN project_edit_workflows w ON w.workflow_id=o.workflow_id
                   WHERE o.option_id=?""",
                (option_id,),
            ).fetchone()
            if (
                row is None
                or int(row["owner_user_id"]) != owner_user_id
                or str(row["stage"]) != "choosing_project"
                or str(row["expires_at"]) <= now
            ):
                raise StateError("project_edit_selection_stale")
            try:
                project = registry.require_project(str(row["project_id"]))
            except KeyError:
                raise StateError("project_edit_selection_stale") from None
            binding = self.connection.execute(
                "SELECT canonical_root FROM project_group_bindings WHERE project_id=?",
                (project.project_id,),
            ).fetchone()
            self.connection.execute(
                """UPDATE project_edit_workflows SET project_id=?,old_display_name=?,
                   old_root=?,old_binding_root=?,stage='choosing_operation',updated_at=?
                   WHERE workflow_id=?""",
                (
                    project.project_id,
                    project.display_name,
                    str(project.root),
                    None if binding is None else str(binding["canonical_root"]),
                    now,
                    str(row["workflow_id"]),
                ),
            )
            workflow_id = str(row["workflow_id"])
        return self.get(workflow_id)

    def operation_markup(self, workflow_id: str) -> dict[str, object]:
        return {
            "inline_keyboard": [
                [{"text": "Изменить имя Hub", "callback_data": f"pe:n:{workflow_id}"}],
                [{"text": "Изменить Git-root", "callback_data": f"pe:r:{workflow_id}"}],
                [{"text": "Отмена", "callback_data": f"pe:x:{workflow_id}"}],
            ]
        }

    def choose_rename(self, owner_user_id: int, workflow_id: str) -> ProjectEditWorkflow:
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_edit_workflows SET operation='rename',stage='awaiting_name',
                   updated_at=? WHERE workflow_id=? AND owner_user_id=?
                   AND stage='choosing_operation' AND expires_at>?""",
                (_now(), workflow_id, owner_user_id, _now()),
            ).rowcount
            if changed != 1:
                raise StateError("project_edit_selection_stale")
        return self.get(workflow_id)

    def set_name(self, owner_user_id: int, workflow_id: str, value: str) -> ProjectEditWorkflow:
        name = _safe_name(value)
        if not 1 <= len(name) <= 128 or not name.isprintable():
            raise StateError("project_edit_name_invalid")
        workflow = self.get(workflow_id)
        if name == workflow.old_display_name:
            raise StateError("project_edit_name_unchanged")
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_edit_workflows SET new_display_name=?,stage='confirming',
                   updated_at=? WHERE workflow_id=? AND owner_user_id=?
                   AND stage='awaiting_name' AND expires_at>?""",
                (name, _now(), workflow_id, owner_user_id, _now()),
            ).rowcount
            if changed != 1:
                raise StateError("project_edit_selection_stale")
        return self.get(workflow_id)

    def _discover_roots(self, workflow: ProjectEditWorkflow) -> tuple[tuple[Path, str, str], ...]:
        if workflow.project_id is None or workflow.old_root is None:
            raise StateError("project_edit_incomplete")
        registry = load_registry(self.registry_path)
        registered = {project.root for project in registry.projects}
        found: dict[Path, tuple[str, str]] = {}
        for index, base in enumerate(registry.allowed_roots, start=1):
            if len(found) >= MAX_ROOT_OPTIONS:
                break
            base = base.resolve(strict=True)
            derived = base / workflow.project_id
            if derived != workflow.old_root:
                try:
                    candidate = validate_relocation_target(
                        registry,
                        project_id=workflow.project_id,
                        current_root=workflow.old_root,
                        target=derived,
                        allow_create=True,
                    )
                except RegistryError:
                    pass
                else:
                    mode = (
                        "create"
                        if not candidate.exists()
                        else "existing"
                        if _exact_git_root(candidate)
                        else "initialize"
                    )
                    label = f"Candidate {len(found) + 1} · Root {index} / {workflow.project_id}"
                    found[candidate] = (mode, label)
            try:
                children = sorted(base.iterdir(), key=lambda item: item.name.casefold())
            except OSError:
                continue
            for child in children:
                if len(found) >= MAX_ROOT_OPTIONS:
                    break
                if child.is_symlink() or not child.is_dir():
                    continue
                try:
                    candidate = child.resolve(strict=True)
                except OSError:
                    continue
                if (
                    candidate == workflow.old_root
                    or candidate in registered
                    or not _exact_git_root(candidate)
                ):
                    continue
                if candidate not in found:
                    child_label = _safe_option_label(child.name, fallback="unnamed", max_length=100)
                    label = f"Candidate {len(found) + 1} · Root {index} / {child_label}"
                    found[candidate] = ("existing", label)
        return tuple((path, mode, label[:160]) for path, (mode, label) in found.items())

    def choose_relocation(self, owner_user_id: int, workflow_id: str) -> ProjectEditWorkflow:
        workflow = self.get(workflow_id)
        if workflow.owner_user_id != owner_user_id or workflow.stage != "choosing_operation":
            raise StateError("project_edit_selection_stale")
        options = self._discover_roots(workflow)
        if not options:
            raise StateError("project_edit_roots_unavailable")
        now = _now()
        with self.state._immediate_transaction():
            changed = self.connection.execute(
                """UPDATE project_edit_workflows SET operation='relocate',stage='choosing_root',
                   updated_at=? WHERE workflow_id=? AND owner_user_id=?
                   AND stage='choosing_operation' AND expires_at>?""",
                (now, workflow_id, owner_user_id, now),
            ).rowcount
            if changed != 1:
                raise StateError("project_edit_selection_stale")
            for root, mode, label in options:
                self.connection.execute(
                    """INSERT INTO project_edit_root_options
                       (option_id,workflow_id,canonical_root,root_mode,safe_label,created_at)
                       VALUES (?,?,?,?,?,?)""",
                    (_token(), workflow_id, str(root), mode, label, now),
                )
        return self.get(workflow_id)

    def root_options(self, workflow_id: str) -> tuple[ProjectEditRootOption, ...]:
        rows = self.connection.execute(
            """SELECT * FROM project_edit_root_options WHERE workflow_id=?
               ORDER BY created_at,option_id""",
            (workflow_id,),
        ).fetchall()
        return tuple(
            ProjectEditRootOption(
                str(row["option_id"]),
                str(row["workflow_id"]),
                Path(str(row["canonical_root"])),
                str(row["root_mode"]),
                str(row["safe_label"]),
            )
            for row in rows
        )

    def root_markup(self, workflow_id: str) -> dict[str, object]:
        rows = [
            [{"text": option.safe_label, "callback_data": f"pe:t:{option.option_id}"}]
            for option in self.root_options(workflow_id)
        ]
        rows.append([{"text": "Отмена", "callback_data": f"pe:x:{workflow_id}"}])
        return {"inline_keyboard": rows}

    def select_root(self, owner_user_id: int, option_id: str) -> ProjectEditWorkflow:
        now = _now()
        with self.state._immediate_transaction():
            row = self.connection.execute(
                """SELECT o.*,w.owner_user_id,w.stage,w.expires_at
                   FROM project_edit_root_options o JOIN project_edit_workflows w
                     ON w.workflow_id=o.workflow_id WHERE o.option_id=?""",
                (option_id,),
            ).fetchone()
            if (
                row is None
                or int(row["owner_user_id"]) != owner_user_id
                or str(row["stage"]) != "choosing_root"
                or str(row["expires_at"]) <= now
            ):
                raise StateError("project_edit_selection_stale")
            self.connection.execute(
                """UPDATE project_edit_workflows SET new_root=?,root_mode=?,
                   stage='confirming',updated_at=? WHERE workflow_id=?""",
                (
                    str(row["canonical_root"]),
                    str(row["root_mode"]),
                    now,
                    str(row["workflow_id"]),
                ),
            )
            workflow_id = str(row["workflow_id"])
        return self.get(workflow_id)

    def confirmation_text(self, workflow: ProjectEditWorkflow) -> str:
        if workflow.project_id is None or workflow.old_display_name is None:
            raise StateError("project_edit_incomplete")
        if workflow.operation == "rename" and workflow.new_display_name is not None:
            return (
                "<b>Изменить локальное имя проекта Hub?</b>\n"
                f"Проект: <code>{html.escape(workflow.project_id)}</code>\n"
                f"Новое имя: {html.escape(workflow.new_display_name)}\n\n"
                "Название Telegram-группы и её числовая привязка не изменятся. "
                "Telegram RPC не выполняется."
            )
        if workflow.operation == "relocate" and workflow.new_root is not None:
            option = self.connection.execute(
                """SELECT safe_label FROM project_edit_root_options
                   WHERE workflow_id=? AND canonical_root=?""",
                (workflow.workflow_id, str(workflow.new_root)),
            ).fetchone()
            if option is None:
                raise StateError("project_edit_incomplete")
            return (
                "<b>Изменить Git-root проекта?</b>\n"
                f"Проект: <code>{html.escape(workflow.project_id)}</code>\n"
                "Новый локально выбранный root: "
                f"<code>{html.escape(str(option['safe_label']))}</code>\n\n"
                "Hub не переносит, не копирует и не удаляет файлы или Git-историю. Старый "
                "root останется на месте. Project ID, Telegram-группа и её числовая привязка "
                "не изменятся. Подключённые provider-сессии и незавершённая работа блокируют "
                "операцию; provider history не перепривязывается."
            )
        raise StateError("project_edit_incomplete")

    def confirmation_markup(self, workflow_id: str) -> dict[str, object]:
        return {
            "inline_keyboard": [
                [{"text": "Подтвердить", "callback_data": f"pe:ok:{workflow_id}"}],
                [{"text": "Отмена", "callback_data": f"pe:x:{workflow_id}"}],
            ]
        }

    def confirm(self, owner_user_id: int, workflow_id: str) -> ProjectEditWorkflow:
        with self.state._immediate_transaction():
            workflow = self.get(workflow_id)
            if workflow.owner_user_id != owner_user_id:
                raise StateError("project_edit_selection_stale")
            if workflow.stage in {"applying", "completed"}:
                return workflow
            changed = self.connection.execute(
                """UPDATE project_edit_workflows SET stage='applying',updated_at=?
                   WHERE workflow_id=? AND owner_user_id=? AND stage='confirming'
                   AND expires_at>?""",
                (_now(), workflow_id, owner_user_id, _now()),
            ).rowcount
            if changed != 1:
                raise StateError("project_edit_selection_stale")
        return self.get(workflow_id)

    def cancel(self, owner_user_id: int, workflow_id: str | None = None) -> bool:
        with self.state._immediate_transaction():
            if workflow_id is None:
                row = self.connection.execute(
                    """SELECT workflow_id FROM project_edit_workflows WHERE owner_user_id=?
                       AND stage IN ('choosing_project','choosing_operation','awaiting_name',
                                     'choosing_root','confirming')
                       ORDER BY created_at DESC LIMIT 1""",
                    (owner_user_id,),
                ).fetchone()
                if row is None:
                    return False
                workflow_id = str(row["workflow_id"])
            return (
                self.connection.execute(
                    """UPDATE project_edit_workflows SET stage='cancelled',updated_at=?
                       WHERE workflow_id=? AND owner_user_id=? AND stage IN
                       ('choosing_project','choosing_operation','awaiting_name','choosing_root',
                        'confirming')""",
                    (_now(), workflow_id, owner_user_id),
                ).rowcount
                == 1
            )

    def _assert_relocation_idle(self, project_id: str) -> None:
        queries = (
            (
                """SELECT 1 FROM agent_sessions s JOIN topics t ON t.topic_id=s.topic_id
                   WHERE t.project_id=? AND s.status!='archived'
                     AND s.provider_session_id IS NOT NULL LIMIT 1""",
                "attached_provider_session",
            ),
            (
                """SELECT 1 FROM agent_sessions s JOIN topics t ON t.topic_id=s.topic_id
                   WHERE t.project_id=? AND s.status!='archived'
                     AND s.writer_mode!='telegram' LIMIT 1""",
                "active_writer",
            ),
            (
                """SELECT 1 FROM turn_dispatches d JOIN topics t ON t.topic_id=d.topic_id
                   WHERE t.project_id=? AND d.status IN ('queued','running') LIMIT 1""",
                "queued_or_running_work",
            ),
            (
                """SELECT 1 FROM provider_jobs j JOIN topics t ON t.topic_id=j.topic_id
                   WHERE t.project_id=? AND j.status IN
                     ('queued','leased','executing','retry_wait') LIMIT 1""",
                "queued_or_running_work",
            ),
            (
                """SELECT 1 FROM provider_jobs j JOIN topics t ON t.topic_id=j.topic_id
                   WHERE t.project_id=? AND j.status='indeterminate' AND NOT EXISTS
                     (SELECT 1 FROM provider_job_resolutions r WHERE r.job_id=j.job_id)
                   LIMIT 1""",
                "unresolved_outcome",
            ),
            (
                """SELECT 1 FROM telegram_outbox o JOIN provider_jobs j ON j.job_id=o.job_id
                   JOIN topics t ON t.topic_id=j.topic_id WHERE t.project_id=?
                   AND o.status IN ('pending','sending') LIMIT 1""",
                "pending_delivery",
            ),
            (
                """SELECT 1 FROM provider_stop_requests r
                   JOIN topics t ON t.topic_id=r.topic_id WHERE t.project_id=?
                   AND r.status='pending' LIMIT 1""",
                "queued_or_running_work",
            ),
            (
                """SELECT 1 FROM provider_jobs j JOIN topics t ON t.topic_id=j.topic_id
                   WHERE t.project_id=? AND j.status='result_ready' LIMIT 1""",
                "pending_delivery",
            ),
            (
                """SELECT 1 FROM provider_progress_deliveries o
                   JOIN provider_jobs j ON j.job_id=o.job_id
                   JOIN topics t ON t.topic_id=j.topic_id WHERE t.project_id=?
                   AND o.status IN ('pending','sending') LIMIT 1""",
                "pending_delivery",
            ),
            (
                """SELECT 1 FROM worktree_lanes w JOIN topics t ON t.topic_id=w.topic_id
                   WHERE t.project_id=? AND w.status='active' LIMIT 1""",
                "active_writer",
            ),
            (
                """SELECT 1 FROM session_connect_workflows WHERE project_id=? AND stage NOT IN
                   ('completed','cancelled','expired','failed','marker_unknown','topic_create_unknown')
                   LIMIT 1""",
                "queued_or_running_work",
            ),
        )
        for query, code in queries:
            if self.connection.execute(query, (project_id,)).fetchone() is not None:
                raise StateError(code)

    @staticmethod
    def _document_project(document: dict[str, object], project_id: str) -> dict[str, object]:
        projects = document.get("projects")
        if not isinstance(projects, list):
            raise RegistryError("projects must be an array")
        matches = [
            value
            for value in projects
            if isinstance(value, dict) and value.get("project_id") == project_id
        ]
        if len(matches) != 1:
            raise RegistryError("project registration changed")
        return matches[0]

    def apply(self, workflow_id: str, *, recovery: bool = False) -> ProjectEditWorkflow:
        workflow = self.get(workflow_id)
        if workflow.stage == "completed":
            return workflow
        if (
            workflow.stage != "applying"
            or workflow.project_id is None
            or workflow.old_display_name is None
            or workflow.old_root is None
            or workflow.operation not in {"rename", "relocate"}
        ):
            raise StateError("project_edit_not_applicable")
        original_document: dict[str, object] | None = None
        wrote_registry = False
        with registry_lock(self.registry_path):
            try:
                with self.state._immediate_transaction():
                    document = _read(self.registry_path)
                    project_data = self._document_project(document, workflow.project_id)
                    current_display = str(project_data.get("display_name", ""))
                    current_root = Path(str(project_data.get("root", ""))).resolve(strict=True)
                    target_display = (
                        workflow.new_display_name
                        if workflow.operation == "rename"
                        else workflow.old_display_name
                    )
                    target_root = (
                        workflow.new_root if workflow.operation == "relocate" else workflow.old_root
                    )
                    if target_display is None or target_root is None:
                        raise StateError("project_edit_incomplete")
                    already_written = (
                        current_display == target_display
                        and current_root == target_root.resolve(strict=False)
                    )
                    if not already_written:
                        if (
                            current_display != workflow.old_display_name
                            or current_root != workflow.old_root.resolve(strict=True)
                        ):
                            raise StateError("project_edit_registration_changed")
                        registry = load_registry(self.registry_path)
                        if workflow.operation == "relocate":
                            self._assert_relocation_idle(workflow.project_id)
                            validated = validate_relocation_target(
                                registry,
                                project_id=workflow.project_id,
                                current_root=workflow.old_root,
                                target=target_root,
                                allow_create=workflow.root_mode in {"create", "initialize"},
                            )
                            if workflow.root_mode in {"create", "initialize"}:
                                validated = prepare_project_root(
                                    validated.parent, workflow.project_id
                                )
                            target_root = validated
                        original_document = copy.deepcopy(document)
                        project_data["display_name"] = target_display
                        project_data["root"] = str(target_root)
                        wrote_registry = True
                        _atomic_write(self.registry_path, document)
                        load_registry(self.registry_path)
                        _after_registry_write()
                    if workflow.operation == "relocate":
                        binding = self.connection.execute(
                            "SELECT canonical_root FROM project_group_bindings WHERE project_id=?",
                            (workflow.project_id,),
                        ).fetchone()
                        if workflow.old_binding_root is None:
                            if binding is not None:
                                raise StateError("project_edit_binding_changed")
                        else:
                            if binding is None:
                                raise StateError("project_edit_binding_changed")
                            binding_root = Path(str(binding["canonical_root"])).resolve(strict=True)
                            old_binding_root = workflow.old_binding_root.resolve(strict=True)
                            target_binding_root = target_root.resolve(strict=True)
                            if binding_root == old_binding_root:
                                changed = self.connection.execute(
                                    """UPDATE project_group_bindings SET canonical_root=?
                                       WHERE project_id=? AND canonical_root=?""",
                                    (
                                        str(target_root),
                                        workflow.project_id,
                                        str(workflow.old_binding_root),
                                    ),
                                ).rowcount
                                if changed != 1:
                                    raise StateError("project_edit_binding_changed")
                            elif binding_root != target_binding_root:
                                raise StateError("project_edit_binding_changed")
                    now = _now()
                    self.connection.execute(
                        """UPDATE project_edit_workflows SET stage='completed',error_code=NULL,
                           new_root=?,completed_at=?,updated_at=?
                           WHERE workflow_id=? AND stage='applying'""",
                        (str(target_root), now, now, workflow_id),
                    )
            except Exception as exc:
                if wrote_registry and original_document is not None:
                    _atomic_write(self.registry_path, original_document)
                    load_registry(self.registry_path)
                error = str(exc)[:128]
                retryable = wrote_registry or error in {
                    "attached_provider_session",
                    "active_writer",
                    "queued_or_running_work",
                    "pending_delivery",
                    "unresolved_outcome",
                }
                with self.state._immediate_transaction():
                    self.connection.execute(
                        """UPDATE project_edit_workflows SET stage=?,error_code=?,updated_at=?
                           WHERE workflow_id=? AND stage='applying'""",
                        (
                            "applying" if recovery else "confirming" if retryable else "failed",
                            error,
                            _now(),
                            workflow_id,
                        ),
                    )
                raise
        return self.get(workflow_id)

    def recover_pending(self) -> tuple[ProjectEditWorkflow, ...]:
        rows = self.connection.execute(
            """SELECT workflow_id FROM project_edit_workflows WHERE stage='applying'
               ORDER BY created_at,workflow_id"""
        ).fetchall()
        recovered: list[ProjectEditWorkflow] = []
        for row in rows:
            recovered.append(self.apply(str(row["workflow_id"]), recovery=True))
        return tuple(recovered)
