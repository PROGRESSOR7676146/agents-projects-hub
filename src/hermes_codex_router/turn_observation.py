"""Bounded read-only reconciliation of accepted Codex turns after stream loss."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .codex_appserver import (
    CodexAppServerClient,
    UnixWebSocketTransport,
)
from .codex_observed_result import ObservedTurnResults
from .hub_config import HubConfig
from .project_resolution import resolve_project_context
from .state import StateError
from .topic_execution import resolve_topic_execution_root


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def owning_read_client(config: HubConfig) -> CodexAppServerClient:
    """Attach only to the owning socket; Controller never launches a provider process."""
    client = CodexAppServerClient(
        UnixWebSocketTransport(config.codex_socket_path, timeout=10),
        approval_policy="never",
        model_provider=config.codex_model_provider,
    )
    client.initialize()
    return client


class TurnObservation(ObservedTurnResults):
    """Keep all state changes on the HubState connection; provider reads stay outside SQL."""

    def topic_unconfirmed(self, topic_id: int) -> bool:
        return (
            self.db.execute(
                """SELECT 1 FROM provider_jobs jobs
               WHERE jobs.topic_id = ? AND jobs.status = 'indeterminate'
                 AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions resolution
                                 WHERE resolution.job_id = jobs.job_id)
                 AND NOT EXISTS (SELECT 1 FROM provider_turn_terminal_evidence terminal
                                 WHERE terminal.job_id = jobs.job_id)
               LIMIT 1""",
                (topic_id,),
            ).fetchone()
            is not None
        )

    def _claim(self) -> tuple[str, str, str, Path] | None:
        codex_agents = tuple(
            agent.agent_id for agent in self.config.agents if agent.runtime == "codex"
        )
        if not codex_agents:
            return None
        placeholders = ",".join("?" for _ in codex_agents)
        now = _now()
        with self.state._immediate_transaction():
            row = self.db.execute(
                f"""SELECT observations.job_id, checkpoint.provider_thread_id,
                          checkpoint.provider_turn_id, checkpoint.project_root
                   FROM provider_turn_observations observations
                   JOIN provider_jobs jobs ON jobs.job_id = observations.job_id
                   JOIN provider_execution_checkpoints checkpoint ON checkpoint.job_id = jobs.job_id
                   JOIN agent_sessions sessions ON sessions.session_id = jobs.session_id
                   WHERE observations.attempt_count < 3 AND observations.next_check_at <= ?
                     AND jobs.status = 'indeterminate' AND jobs.agent_id IN ({placeholders})
                     AND sessions.status IN ('active','satellite') AND sessions.writer_mode = 'telegram'
                     AND sessions.topic_id=jobs.topic_id AND sessions.agent_id=jobs.agent_id
                     AND sessions.generation = jobs.session_generation
                     AND sessions.provider_session_id = checkpoint.provider_thread_id
                     AND checkpoint.provider_turn_id IS NOT NULL
                     AND NOT EXISTS (SELECT 1 FROM provider_turn_terminal_evidence terminal
                                     WHERE terminal.job_id = jobs.job_id)
                     AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions resolution
                                     WHERE resolution.job_id = jobs.job_id)
                   ORDER BY observations.next_check_at, observations.job_id LIMIT 1""",
                (now, *codex_agents),
            ).fetchone()
            if row is None:
                return None
            self.db.execute(
                """UPDATE provider_turn_observations
                   SET attempt_count = attempt_count + 1,
                       next_check_at = ?, updated_at = ? WHERE job_id = ?""",
                (
                    (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
                    now,
                    row["job_id"],
                ),
            )
            return (
                str(row["job_id"]),
                str(row["provider_thread_id"]),
                str(row["provider_turn_id"]),
                Path(str(row["project_root"])),
            )

    def run_once(self, client_factory: Callable[[], CodexAppServerClient]) -> bool:
        claimed = self._claim()
        if claimed is None:
            return False
        self._observe(claimed, client_factory, refund_deferred_attempt=True)
        return True

    def observe_topic(
        self, topic_id: int, client_factory: Callable[[], CodexAppServerClient]
    ) -> bool:
        """User-initiated /local check; it never resumes or starts a turn."""
        rows = self.db.execute(
            """SELECT jobs.job_id, checkpoint.provider_thread_id,
                      checkpoint.provider_turn_id, checkpoint.project_root
               FROM provider_jobs jobs
               JOIN provider_execution_checkpoints checkpoint ON checkpoint.job_id = jobs.job_id
               WHERE jobs.topic_id = ? AND jobs.status = 'indeterminate'
                 AND checkpoint.provider_turn_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions resolution
                                 WHERE resolution.job_id = jobs.job_id)
                 AND NOT EXISTS (SELECT 1 FROM provider_turn_terminal_evidence terminal
                                 WHERE terminal.job_id = jobs.job_id)""",
            (topic_id,),
        ).fetchall()
        if len(rows) > 1:
            raise StateError("multiple unconfirmed turns require separate review")
        if not rows:
            return False
        row = rows[0]
        self._observe(
            (
                str(row["job_id"]),
                str(row["provider_thread_id"]),
                str(row["provider_turn_id"]),
                Path(str(row["project_root"])),
            ),
            client_factory,
        )
        return True

    def _observe(
        self,
        claimed: tuple[str, str, str, Path],
        client_factory: Callable[[], CodexAppServerClient],
        *,
        refund_deferred_attempt: bool = False,
    ) -> None:
        job_id, thread_id, turn_id, root = claimed
        try:
            job = self.state.get_provider_job(job_id)
            topic = self.state.get_topic(job.topic_id)
            resolved = resolve_project_context(
                self.config,
                self.state,
                chat_id=topic.chat_id,
                expected_project_id=topic.project_id,
                expected_root=root,
            )
            if resolve_topic_execution_root(self.state, resolved.registry, topic) != root:
                raise StateError("observation project root changed")
            client = client_factory()
            try:
                outcome = client.read_turn_outcome(thread_id=thread_id, turn_id=turn_id, cwd=root)
            finally:
                client.close()
        except Exception:
            self._record_uncertain(job_id, "unknown")
            return
        self.apply_outcome(
            job_id,
            thread_id,
            turn_id,
            root,
            outcome,
            refund_deferred_attempt=refund_deferred_attempt,
        )
