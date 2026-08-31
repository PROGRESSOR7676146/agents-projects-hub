from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.external_runtime import ExternalTurnResult
from hermes_codex_router.external_worker import ExternalQueueWorker
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    HubTelegramBot,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.outbox_sender import TelegramOutboxSender
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.state import HubState

CHAT_ID = -1001234567890
OWNER_ID = 42


class RecordingAdapter:
    def __init__(self, runtime: str) -> None:
        self.runtime = runtime
        self.calls: list[str] = []

    def run_turn(self, **kwargs: object) -> ExternalTurnResult:
        prompt = str(kwargs["prompt"])
        self.calls.append(prompt)
        return ExternalTurnResult(
            self.runtime,
            f"{self.runtime} completed: {prompt}",
            f"{self.runtime}-session-{len(self.calls)}",
            "fictional-model",
        )


class RecordingBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, int, str]] = []

    def send_html(self, chat_id: int, thread_id: int, html: str, **_kwargs: object) -> int:
        self.sent.append((chat_id, thread_id, html))
        return len(self.sent)

    def send_chat_action(self, _chat_id: int, _thread_id: int, _action: str = "typing") -> None:
        pass


class FaultMatrixHarness:
    """Reusable fictional topology shared by parent tests and child actors."""

    chat_id = CHAT_ID

    def __init__(self, base: Path) -> None:
        self.base = base
        project_root = base / "example-project"
        (project_root / ".git").mkdir(parents=True, exist_ok=True)
        agents = (
            AgentDefinition(
                "codex",
                "Codex",
                "example_codex_bot",
                "codex",
                None,
                True,
                False,
                "fictional-codex-model",
                "high",
            ),
            AgentDefinition(
                "opencode",
                "OpenCode",
                "example_opencode_bot",
                "opencode",
                None,
                False,
                False,
                "provider-selected",
                "high",
                executable="example-opencode",
            ),
            AgentDefinition(
                "antigravity",
                "Antigravity",
                "example_antigravity_bot",
                "antigravity",
                None,
                False,
                False,
                "provider-selected",
                "high",
                executable="example-antigravity",
            ),
        )
        self.config = HubConfig(
            schema_version=1,
            owner_user_ids=(OWNER_ID,),
            registry_path=base / "projects.json",
            state_path=base / "state.db",
            codex_socket_path=base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Example Linux"),
            projects=(ProjectBinding("example-project", CHAT_ID),),
            agents=agents,
            hub_bot=HubTelegramBot("example_hub_bot", base / "unused-hub-token"),
            dispatch_mode="queue",
            queue_runtime="external",
            outbox_runtime="external",
            external_worker_agent_ids=("codex", "opencode", "antigravity"),
        )
        self.registry = ProjectRegistry(
            1,
            (base,),
            (Project("example-project", "Example Project", "Example", project_root),),
        )

    def controller(
        self,
        *,
        ingress_identity: str = "hub",
        direct_messages_only: bool = False,
    ) -> ProjectHubService:
        service = cast(Any, ProjectHubService.__new__(ProjectHubService))
        service.config = self.config
        service.registry = self.registry
        service.state = HubState.open(self.config.state_path)
        service.agent = self.config.require_agent("codex")
        service.telegram = RecordingBot()
        service.usernames = {
            agent.agent_id: agent.telegram_username for agent in self.config.agents
        }
        service.external_services = {}
        service.ingress_identity = ingress_identity
        service.direct_messages_only = direct_messages_only
        service._publishes_controller_health = not direct_messages_only
        service.supervisor = None
        service._codex_client = None
        service._codex_telegram = None
        service._queue_stop = threading.Event()
        service._queue_thread = None
        service._outbox_stop = threading.Event()
        service._outbox_thread = None
        service._outbox_agent_cursor = 0
        service._stop = threading.Event()
        service._health_started_at = datetime.now(timezone.utc)
        service._health_process_start_marker = uuid.uuid4().hex
        service._health_last_success_at = None
        service._health_last_error_code = None
        service._health_last_publish_monotonic = 0.0
        return cast(ProjectHubService, service)

    def update(self, message_id: int, thread_id: int, text: str) -> dict[str, object]:
        return {
            "update_id": message_id,
            "message": {
                "message_id": message_id,
                "message_thread_id": thread_id,
                "is_topic_message": True,
                "from": {"id": OWNER_ID, "is_bot": False},
                "chat": {"id": CHAT_ID, "type": "supergroup", "title": "Example"},
                "text": text,
            },
        }

    def worker(self, agent_id: str, adapter: object) -> ExternalQueueWorker:
        return ExternalQueueWorker(
            self.config,
            agent_id,
            registry=self.registry,
            adapter=cast(Any, adapter),
            worker_id=f"fictional-{agent_id}-worker",
        )

    def sender(self, **bots: object) -> TelegramOutboxSender:
        defaults: dict[str, object] = {
            "codex": RecordingBot(),
            "opencode": RecordingBot(),
            "antigravity": RecordingBot(),
        }
        defaults.update(bots)
        return TelegramOutboxSender(
            self.config,
            telegram_bots=cast(dict[str, Any], defaults),
            sender_id="fictional-sender",
        )

    def one_job(self, thread_id: int):
        state = HubState.open(self.config.state_path)
        try:
            topic = state.find_topic(CHAT_ID, thread_id)
            assert topic is not None
            jobs = state.provider_jobs_for_topic(topic.topic_id)
            assert len(jobs) == 1
            return jobs[0]
        finally:
            state.close()
