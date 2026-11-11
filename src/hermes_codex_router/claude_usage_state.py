"""Read the saved Claude usage summary of a session's latest finished turn."""

from __future__ import annotations

from .claude_usage import last_context_tokens, usage_from_metadata
from .state import HubState

_IN_FLIGHT = ("queued", "leased", "executing", "retry_wait")


class ClaudeUsageStatus:
    def __init__(self, state: HubState) -> None:
        self.connection = state._connection

    def last_context(self, session_id: str) -> int | None:
        """Return the last request context of the latest finished turn, else unknown.

        A failed, stopped or uncertain latest turn may have changed the context
        without reporting it, so an older successful snapshot is not reused.
        """
        row = self.connection.execute(
            f"""SELECT jobs.status, results.safe_metadata_json
                FROM provider_jobs jobs
                LEFT JOIN provider_job_results results ON results.job_id = jobs.job_id
                WHERE jobs.topic_id = (
                        SELECT topic_id FROM agent_sessions WHERE session_id = ?
                      )
                  AND jobs.session_id = ?
                  AND jobs.status NOT IN ({",".join("?" for _ in _IN_FLIGHT)})
                ORDER BY jobs.topic_sequence DESC LIMIT 1""",
            (session_id, session_id, *_IN_FLIGHT),
        ).fetchone()
        if row is None or row["status"] not in {"result_ready", "completed"}:
            return None
        return last_context_tokens(usage_from_metadata(row["safe_metadata_json"]))


__all__ = ["ClaudeUsageStatus"]
