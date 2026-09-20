from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class AcceptanceActorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AcceptanceActorConfig:
    api_id: int
    api_hash_file: Path
    session_path: Path
    expected_user_id: int | None
    telegram_chat_id: int
    telegram_thread_id: int
    hub_username: str
    provider_usernames: tuple[str, ...]
    checks: tuple[str, ...]
    timeout_seconds: int
    artifacts_dir: Path
    provider_agent_ids: tuple[str, ...] = ()
    state_path: Path | None = None
    allow_service_restart: bool = False


@dataclass(frozen=True, slots=True)
class AcceptanceCheckResult:
    check: str
    target: str
    ok: bool
    response_message_id: int | None
    detail: str
