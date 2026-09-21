from __future__ import annotations

import re
import sqlite3
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable, Sequence

from .incoming_materials import (
    IncomingMaterialDraft,
    IncomingMaterialError,
    IncomingMaterialRecord,
    bound_material_drafts,
    cleanup_rejected_draft_inputs,
)

StateErrorFactory = Callable[[str], Exception]
TransactionFactory = Callable[[], AbstractContextManager[None]]


class IncomingMaterialsStateFacade:
    """Incoming-material queries on the HubState-owned SQLite connection."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        state_path: Path | None,
        *,
        transaction: TransactionFactory,
        state_error: StateErrorFactory,
    ) -> None:
        self._connection = connection
        self._state_path = state_path
        self._transaction = transaction
        self._state_error = state_error

    def _bounded(self, value: str, *, name: str, maximum: int) -> str:
        normalized = value.strip()
        if not normalized or len(normalized) > maximum:
            raise self._state_error(f"invalid {name}")
        return normalized

    def _optional_bounded(self, value: str | None, *, name: str, maximum: int) -> str | None:
        if value is None:
            return None
        return self._bounded(value, name=name, maximum=maximum)

    @staticmethod
    def _record(row: sqlite3.Row) -> IncomingMaterialRecord:
        return IncomingMaterialRecord(
            material_id=str(row["material_id"]),
            job_id=None if row["job_id"] is None else str(row["job_id"]),
            topic_id=int(row["topic_id"]),
            project_id=str(row["project_id"]),
            execution_scope=str(row["execution_scope"]),
            agent_id=None if row["agent_id"] is None else str(row["agent_id"]),
            session_id=None if row["session_id"] is None else str(row["session_id"]),
            session_generation=(
                None if row["session_generation"] is None else int(row["session_generation"])
            ),
            chat_id=int(row["chat_id"]),
            message_id=int(row["message_id"]),
            attachment_index=int(row["attachment_index"]),
            media_group_id=row["media_group_id"],
            origin=str(row["origin"]),
            kind=str(row["kind"]),
            content_kind=row["content_kind"],
            file_unique_id=row["file_unique_id"],
            display_name=str(row["display_name"]),
            mime_type=row["mime_type"],
            declared_size=row["declared_size"],
            storage_path=row["storage_path"],
            byte_size=row["byte_size"],
            sha256=row["sha256"],
            status=str(row["status"]),
            unavailable_code=row["unavailable_code"],
            unavailable_detail=row["unavailable_detail"],
        )

    def insert(
        self,
        *,
        job_id: str | None,
        topic_id: int,
        chat_id: int,
        message_id: int,
        agent_id: str | None,
        session_id: str | None,
        session_generation: int | None,
        materials: Sequence[IncomingMaterialDraft],
        timestamp: str,
        origin: str | None = None,
    ) -> None:
        if not materials:
            return
        topic = self._connection.execute(
            "SELECT project_id, execution_scope FROM topics WHERE topic_id = ?",
            (topic_id,),
        ).fetchone()
        if topic is None:
            raise self._state_error(f"unknown topic_id: {topic_id}")
        if job_id is not None:
            aggregate = self._connection.execute(
                """SELECT COUNT(*), COALESCE(SUM(byte_size), 0)
                   FROM incoming_materials WHERE job_id = ?""",
                (job_id,),
            ).fetchone()
        else:
            aggregate = self._connection.execute(
                """SELECT COUNT(*), COALESCE(SUM(byte_size), 0)
                   FROM incoming_materials
                   WHERE job_id IS NULL AND topic_id = ? AND session_id IS ?
                     AND session_generation IS ?""",
                (topic_id, session_id, session_generation),
            ).fetchone()
        assert aggregate is not None
        bounded_materials = bound_material_drafts(
            materials,
            existing_count=int(aggregate[0]),
            existing_bytes=int(aggregate[1]),
        )
        if any(
            before.storage_path is not None and after.storage_path is None
            for before, after in zip(materials, bounded_materials, strict=True)
        ):
            if self._state_path is None:
                raise self._state_error("incoming material cleanup requires a state path")
            cleanup_rejected_draft_inputs(
                materials,
                bounded_materials,
                state_path=self._state_path,
            )
        execution_scope = topic["execution_scope"] or f"project:{topic['project_id']}"
        material_origin = origin or ("direct" if chat_id > 0 else "topic")
        for material in bounded_materials:
            name = self._bounded(material.display_name, name="material display name", maximum=128)
            kind = self._bounded(material.kind, name="material kind", maximum=32)
            media_group_id = self._optional_bounded(
                material.media_group_id, name="media group id", maximum=256
            )
            unique_id = self._optional_bounded(
                material.file_unique_id, name="Telegram file unique id", maximum=512
            )
            mime_type = self._optional_bounded(
                material.mime_type, name="material MIME type", maximum=256
            )
            code = self._optional_bounded(
                material.unavailable_code, name="material error code", maximum=64
            )
            detail = self._optional_bounded(
                material.unavailable_detail, name="material error detail", maximum=500
            )
            storage_path = (
                self._bounded(
                    str(material.storage_path), name="material storage path", maximum=4096
                )
                if material.storage_path is not None
                else None
            )
            if material.status == "stored":
                if (
                    material.content_kind not in {"text", "image"}
                    or storage_path is None
                    or material.byte_size is None
                    or material.sha256 is None
                    or re.fullmatch(r"[0-9a-f]{64}", material.sha256) is None
                ):
                    raise self._state_error("stored material metadata is incomplete")
            elif material.status == "unavailable":
                if code is None or detail is None:
                    raise self._state_error("unavailable material has no bounded reason")
            else:
                raise self._state_error("invalid incoming material status")
            self._connection.execute(
                """INSERT INTO incoming_materials (
                       material_id, job_id, topic_id, project_id, execution_scope,
                       agent_id, session_id, session_generation, chat_id, message_id,
                       attachment_index, media_group_id, origin, kind, content_kind,
                       file_unique_id, display_name, mime_type, declared_size,
                       storage_path, byte_size, sha256, status, unavailable_code,
                       unavailable_detail, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                             ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    job_id,
                    topic_id,
                    str(topic["project_id"]),
                    str(execution_scope),
                    agent_id,
                    session_id,
                    session_generation,
                    chat_id,
                    message_id,
                    material.attachment_index,
                    media_group_id,
                    material_origin,
                    kind,
                    material.content_kind,
                    unique_id,
                    name,
                    mime_type,
                    material.declared_size,
                    storage_path,
                    material.byte_size,
                    material.sha256,
                    material.status,
                    code,
                    detail,
                    timestamp,
                ),
            )

    def attach_forwarded(
        self,
        *,
        job_id: str,
        topic_id: int,
        agent_id: str,
        session_id: str,
        session_generation: int,
    ) -> None:
        topic = self._connection.execute(
            "SELECT project_id, execution_scope FROM topics WHERE topic_id = ?",
            (topic_id,),
        ).fetchone()
        if topic is None:
            raise self._state_error(f"unknown topic_id: {topic_id}")
        execution_scope = topic["execution_scope"] or f"project:{topic['project_id']}"
        self._connection.execute(
            """UPDATE incoming_materials SET job_id = ?
               WHERE job_id IS NULL AND topic_id = ? AND project_id = ?
                 AND execution_scope = ? AND agent_id = ? AND session_id = ?
                 AND session_generation = ?""",
            (
                job_id,
                topic_id,
                topic["project_id"],
                execution_scope,
                agent_id,
                session_id,
                session_generation,
            ),
        )

    def for_job(self, job_id: str) -> tuple[IncomingMaterialRecord, ...]:
        binding = self._connection.execute(
            """SELECT jobs.topic_id, jobs.chat_id, jobs.agent_id, jobs.session_id,
                      jobs.session_generation, topics.project_id, topics.execution_scope
               FROM provider_jobs jobs JOIN topics ON topics.topic_id = jobs.topic_id
               WHERE jobs.job_id = ?""",
            (job_id,),
        ).fetchone()
        if binding is None:
            raise self._state_error(f"unknown provider job: {job_id}")
        rows = self._connection.execute(
            """SELECT materials.* FROM incoming_materials materials
               LEFT JOIN provider_job_inputs inputs
                 ON inputs.job_id = materials.job_id
                AND inputs.chat_id = materials.chat_id
                AND inputs.message_id = materials.message_id
               WHERE materials.job_id = ?
               ORDER BY COALESCE(inputs.part_index, 1), materials.attachment_index""",
            (job_id,),
        ).fetchall()
        records = tuple(self._record(row) for row in rows)
        execution_scope = binding["execution_scope"] or f"project:{binding['project_id']}"
        if any(
            (
                record.topic_id,
                record.project_id,
                record.execution_scope,
                record.agent_id,
                record.session_id,
                record.session_generation,
                record.chat_id,
            )
            != (
                int(binding["topic_id"]),
                str(binding["project_id"]),
                str(execution_scope),
                str(binding["agent_id"]),
                str(binding["session_id"]),
                int(binding["session_generation"]),
                int(binding["chat_id"]),
            )
            for record in records
        ):
            raise IncomingMaterialError("incoming material binding changed")
        return records

    def pending(self, topic_id: int) -> tuple[IncomingMaterialRecord, ...]:
        rows = self._connection.execute(
            """SELECT * FROM incoming_materials
               WHERE topic_id = ? AND job_id IS NULL
               ORDER BY created_at, message_id, attachment_index""",
            (topic_id,),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def delete_pending(self, topic_id: int, material_ids: Sequence[str]) -> int:
        identifiers = tuple(
            self._bounded(value, name="incoming material id", maximum=128) for value in material_ids
        )
        if not identifiers:
            return 0
        placeholders = ", ".join("?" for _ in identifiers)
        with self._transaction():
            cursor = self._connection.execute(
                f"""DELETE FROM incoming_materials
                    WHERE topic_id = ? AND job_id IS NULL
                      AND material_id IN ({placeholders})""",
                (topic_id, *identifiers),
            )
        return cursor.rowcount

    def stored_for_terminal_jobs(self, topic_id: int) -> tuple[IncomingMaterialRecord, ...]:
        rows = self._connection.execute(
            """SELECT materials.* FROM incoming_materials materials
               JOIN provider_jobs jobs ON jobs.job_id = materials.job_id
               WHERE materials.topic_id = ?
                 AND materials.status IN ('stored', 'consumed')
                 AND jobs.status IN ('result_ready', 'completed', 'failed', 'cancelled')
               ORDER BY materials.created_at, materials.message_id,
                        materials.attachment_index""",
            (topic_id,),
        ).fetchall()
        return tuple(self._record(row) for row in rows)

    def mark_discarded(
        self,
        material_ids: Sequence[str],
        *,
        code: str,
        detail: str,
    ) -> int:
        identifiers = tuple(
            self._bounded(value, name="incoming material id", maximum=128) for value in material_ids
        )
        if not identifiers:
            return 0
        error_code = self._bounded(code, name="material error code", maximum=64)
        error_detail = self._bounded(detail, name="material error detail", maximum=500)
        placeholders = ", ".join("?" for _ in identifiers)
        with self._transaction():
            cursor = self._connection.execute(
                f"""UPDATE incoming_materials
                    SET status = 'unavailable', content_kind = NULL,
                        storage_path = NULL, byte_size = NULL, sha256 = NULL,
                        unavailable_code = ?, unavailable_detail = ?, consumed_at = NULL
                    WHERE status = 'stored' AND material_id IN ({placeholders})""",
                (error_code, error_detail, *identifiers),
            )
        return cursor.rowcount

    def job_has_materials(self, job_id: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM incoming_materials WHERE job_id = ? LIMIT 1",
            (job_id,),
        ).fetchone()
        return row is not None

    def mark_job_consumed(self, job_id: str, *, timestamp: str) -> None:
        self._connection.execute(
            """UPDATE incoming_materials
               SET status = 'consumed', consumed_at = ?
               WHERE job_id = ? AND status = 'stored'""",
            (timestamp, job_id),
        )
