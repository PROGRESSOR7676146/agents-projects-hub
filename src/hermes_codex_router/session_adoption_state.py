"""Local, transactional ownership of externally created Codex threads."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .codex_appserver import validate_codex_thread_id
from .state import HubState, SessionRecord, StateError, TopicRecord, _bounded, _now


@dataclass(frozen=True, slots=True)
class AdoptionRequest:
    project_id: str
    chat_id: int
    thread_id: int
    provider_thread_id: str
    canonical_root: Path
    model: str
    effort: str
    replaces_session_id: str | None = None


@dataclass(frozen=True, slots=True)
class CodexOrigin:
    session_id: str
    provider_thread_id: str
    project_id: str
    canonical_root: Path
    model_provider: str
    replaces_session_id: str | None
    activation_message_id: int | None
    context_floor_turn_id: int


@dataclass(frozen=True, slots=True)
class AdoptionTarget:
    topic: TopicRecord
    session: SessionRecord | None
    already_attached: bool = False


@dataclass(frozen=True, slots=True)
class AttachedSession(AdoptionTarget):
    session: SessionRecord


class CodexSessionOrigins:
    def __init__(self, state: HubState) -> None:
        self.state = state
        self.connection = state._connection

    def get(self, session_id: str) -> CodexOrigin | None:
        row = self.connection.execute(
            "SELECT * FROM codex_session_origins WHERE session_id = ?", (session_id,)
        ).fetchone()
        return (
            None
            if row is None
            else CodexOrigin(
                str(row["session_id"]),
                str(row["provider_thread_id"]),
                str(row["project_id"]),
                Path(row["canonical_root"]),
                str(row["model_provider"]),
                row["replaces_session_id"],
                row["activation_message_id"],
                int(row["context_floor_turn_id"]),
            )
        )

    def _exists(self, query: str, *params: object) -> bool:
        return self.connection.execute(query, params).fetchone() is not None

    def require(self, session_id: str) -> CodexOrigin:
        origin = self.get(session_id)
        if origin is None:
            raise StateError("Codex origin is missing")
        return origin

    def _topic_idle(self, topic_id: int) -> None:
        checks = (
            (
                "SELECT 1 FROM turn_dispatches WHERE topic_id = ? AND status IN ('queued','running')",
                "target_busy",
            ),
            (
                "SELECT 1 FROM provider_jobs WHERE topic_id = ? AND status IN ('queued','leased','executing','retry_wait','result_ready')",
                "target_busy",
            ),
            (
                "SELECT 1 FROM provider_jobs j WHERE j.topic_id = ? AND j.status = 'indeterminate' AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions r WHERE r.job_id = j.job_id)",
                "unresolved_work",
            ),
            (
                "SELECT 1 FROM telegram_outbox o JOIN provider_jobs j ON j.job_id=o.job_id WHERE j.topic_id=? AND o.status != 'delivered'",
                "pending_delivery",
            ),
            (
                "SELECT 1 FROM provider_progress_deliveries o JOIN provider_jobs j ON j.job_id=o.job_id WHERE j.topic_id=? AND o.status NOT IN ('delivered','superseded')",
                "pending_delivery",
            ),
            (
                "SELECT 1 FROM provider_stop_requests WHERE topic_id=? AND status='pending'",
                "pending_stop",
            ),
            (
                "SELECT 1 FROM agent_sessions WHERE topic_id=? AND status != 'archived' AND writer_mode != 'telegram'",
                "local_writer",
            ),
        )
        for query, reason in checks:
            if self._exists(query + " LIMIT 1", topic_id):
                raise StateError(reason)

    def preview(self, request: AdoptionRequest) -> AdoptionTarget:
        """Read predicates only. Caller proves config/root/backend outside SQLite."""
        validate_codex_thread_id(request.provider_thread_id)
        _bounded(request.project_id, name="project_id", maximum=48)
        _bounded(request.model, name="model", maximum=200)
        _bounded(request.effort, name="effort", maximum=64)
        if (
            type(request.chat_id) is not int
            or not -(2**63) <= request.chat_id < 0
            or type(request.thread_id) is not int
            or not 0 < request.thread_id < 2**63
        ):
            raise StateError("invalid_topic_identity")
        if not request.canonical_root.is_absolute() or len(str(request.canonical_root)) > 4096:
            raise StateError("invalid_root")
        topic = self.state.find_topic(request.chat_id, request.thread_id)
        if topic is None or topic.project_id != request.project_id:
            raise StateError("topic_mismatch")
        if self._exists(
            "SELECT 1 FROM worktree_lanes WHERE topic_id=? AND status='active'", topic.topic_id
        ):
            raise StateError("lane_not_supported")
        current = self.state.active_session(topic.topic_id)
        origin_row = self.connection.execute(
            "SELECT session_id FROM codex_session_origins WHERE provider_thread_id=?",
            (request.provider_thread_id,),
        ).fetchone()
        if origin_row is not None:
            origin = self.get(str(origin_row["session_id"]))
            assert origin is not None
            if current is None or current.session_id != origin.session_id:
                raise StateError("binding_superseded")
            if (
                origin.project_id != request.project_id
                or origin.canonical_root != request.canonical_root
                or origin.replaces_session_id != request.replaces_session_id
                or current.model != request.model
                or current.effort != request.effort
            ):
                raise StateError("binding_conflict")
            return AdoptionTarget(topic, current, True)
        if request.replaces_session_id is not None:
            if (
                current is None
                or current.agent_id != "codex"
                or current.session_id != request.replaces_session_id
            ):
                raise StateError("target_changed")
            if current.provider_session_id == request.provider_thread_id:
                return AdoptionTarget(topic, current, True)
        elif current is not None and (
            current.agent_id != "codex" or current.provider_session_id is not None
        ):
            raise StateError("target_not_empty")
        for query in (
            "SELECT 1 FROM agent_sessions WHERE provider_session_id=?",
            "SELECT 1 FROM provider_jobs WHERE provider_session_id=?",
            "SELECT 1 FROM provider_execution_checkpoints WHERE provider_thread_id=?",
            "SELECT 1 FROM provider_job_results WHERE provider_session_id=?",
        ):
            if self._exists(query + " LIMIT 1", request.provider_thread_id):
                raise StateError("source_already_bound")
        self._topic_idle(topic.topic_id)
        if request.replaces_session_id is None:
            for table in (
                "external_turn_excerpts",
                "provider_jobs",
                "turn_dispatches",
                "pending_handoffs",
            ):
                if self._exists(f"SELECT 1 FROM {table} WHERE topic_id=? LIMIT 1", topic.topic_id):
                    raise StateError("target_not_empty")
            if self._exists(
                "SELECT 1 FROM agent_sessions WHERE topic_id=? AND (status != 'active' OR agent_id != 'codex' OR provider_session_id IS NOT NULL) LIMIT 1",
                topic.topic_id,
            ):
                raise StateError("target_not_empty")
        # Registry roots are unique. Retained origins and execution checkpoints
        # can also identify this root under an older registration; no filesystem
        # inspection belongs inside this transaction.
        other_topics = self.connection.execute(
            """SELECT topic_id FROM topics WHERE topic_id != ? AND
               (project_id=? OR topic_id IN (SELECT s.topic_id FROM agent_sessions s
                  JOIN codex_session_origins o ON o.session_id=s.session_id WHERE o.canonical_root=?)
                OR topic_id IN (SELECT j.topic_id FROM provider_jobs j
                  JOIN provider_execution_checkpoints c ON c.job_id=j.job_id WHERE c.project_root=?))""",
            (
                topic.topic_id,
                request.project_id,
                str(request.canonical_root),
                str(request.canonical_root),
            ),
        ).fetchall()
        for other in other_topics:
            self._topic_idle(int(other["topic_id"]))
        return AdoptionTarget(topic, current)

    def attach(
        self, request: AdoptionRequest, *, expected_session_id: str | None
    ) -> AttachedSession:
        with self.state._immediate_transaction():
            # Compare the active binding first, so a stale preview cannot adopt
            # a freshly reset placeholder. Exact receipt repeats are exempt.
            topic = self.state.find_topic(request.chat_id, request.thread_id)
            current = self.state.active_session(topic.topic_id) if topic is not None else None
            if (
                current.session_id if current else None
            ) != expected_session_id and not self._exists(
                "SELECT 1 FROM codex_session_origins WHERE provider_thread_id=?",
                request.provider_thread_id,
            ):
                raise StateError("target_changed")
            target = self.preview(request)
            if target.already_attached:
                assert target.session is not None
                return AttachedSession(target.topic, target.session, True)
            if (target.session.session_id if target.session else None) != expected_session_id:
                raise StateError("target_changed")
            now = _now()
            if target.session is not None:
                self.connection.execute(
                    "UPDATE agent_sessions SET status='archived', updated_at=? WHERE session_id=?",
                    (now, target.session.session_id),
                )
            session = self.state._insert_session(
                target.topic.topic_id, "codex", request.model, request.effort, "active"
            )
            self.connection.execute(
                "UPDATE agent_sessions SET provider_session_id=?, writer_mode='local' WHERE session_id=?",
                (request.provider_thread_id, session.session_id),
            )
            self.connection.execute(
                """INSERT INTO codex_session_origins (session_id, provider_thread_id, project_id,
                   canonical_root, model_provider, created_at, replaces_session_id)
                   VALUES (?, ?, ?, ?, 'openai', ?, ?)""",
                (
                    session.session_id,
                    request.provider_thread_id,
                    request.project_id,
                    str(request.canonical_root),
                    now,
                    request.replaces_session_id,
                ),
            )
            self.connection.execute(
                "UPDATE topics SET active_agent_id='codex', updated_at=? WHERE topic_id=?",
                (now, target.topic.topic_id),
            )
            return AttachedSession(
                self.state.get_topic(target.topic.topic_id),
                self.state.get_session(session.session_id),
            )

    def require_admission(self, session_id: str, message_id: int) -> None:
        origin = self.get(session_id)
        if origin is not None and (
            origin.activation_message_id is None or message_id <= origin.activation_message_id
        ):
            raise StateError("input_before_session_activation")

    def activate(self, session_id: str, message_id: int, topic_id: int) -> None:
        origin = self.get(session_id)
        if origin is None or origin.activation_message_id is not None:
            return
        floor = self.connection.execute(
            "SELECT COALESCE(MAX(turn_id),0) FROM external_turn_excerpts WHERE topic_id=?",
            (topic_id,),
        ).fetchone()[0]
        self.connection.execute(
            "UPDATE codex_session_origins SET activation_message_id=?, context_floor_turn_id=? WHERE session_id=?",
            (message_id, floor, session_id),
        )

    def forwarded_boundary(self, topic_id: int, agent_id: str) -> tuple[int, int]:
        row = self.connection.execute(
            """SELECT o.* FROM codex_session_origins o JOIN agent_sessions s ON s.session_id=o.session_id
               WHERE s.topic_id=? AND s.agent_id=? AND s.status IN ('active','satellite')""",
            (topic_id, agent_id),
        ).fetchone()
        if row is None:
            return 0, 0
        if row["activation_message_id"] is None:
            return 2**63 - 1, 2**63 - 1
        return int(row["context_floor_turn_id"]), int(row["activation_message_id"])
