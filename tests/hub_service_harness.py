"""One reusable ProjectHubService test harness with fake transports.

Service tests historically built the Controller with ``__new__`` and a
hand-picked attribute set in each module. Characterization tests use this
single builder so that moving dispatcher branches into collaborators changes
one fixture, not every test.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.codex_appserver import CodexThread, RateLimits, TurnResult
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.state import HubState, SessionRecord, TopicRecord
from tests.git_fixtures import init_git_root

CHAT_ID = -1001234567890
THREAD_ID = 77
OWNER_ID = 42

CODEX = AgentDefinition(
    "codex", "Codex", "example_codex_bot", "codex", None, True, False, "gpt-5.6-sol", "high"
)
ANTIGRAVITY = AgentDefinition(
    "antigravity",
    "Antigravity",
    "example_antigravity_bot",
    "antigravity",
    None,
    True,
    False,
    "gemini-example",
    "high",
)


class RecordingTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.markups: list[object | None] = []

    def send_html(self, _chat_id: int, _thread_id: int, text: str, **kwargs: object) -> int:
        self.sent.append(text)
        self.markups.append(kwargs.get("reply_markup"))
        return len(self.sent)

    def answer_callback(self, _callback_id: str, _text: str = "") -> None:
        pass

    def send_chat_action(self, *_args: object, **_kwargs: object) -> None:
        pass


class IdleCodexClient:
    """A Codex client that completes every turn with visible text."""

    def __init__(self) -> None:
        self.turns = 0

    def start_thread(self, **kwargs: object) -> CodexThread:
        return CodexThread("thread-1", Path(str(kwargs["cwd"])), "gpt-5.6-sol", "openai")

    def resume_thread(self, **kwargs: object) -> CodexThread:
        return self.start_thread(**kwargs)

    def start_turn(self, **_kwargs: object) -> str:
        self.turns += 1
        return f"turn-{self.turns}"

    def wait_for_turn(self, _turn_id: str) -> TurnResult:
        return TurnResult("Visible answer", 1000, 100)

    def consume_completed_connection(self, *, thread_id: str, turn_id: str) -> bool:
        return False

    def read_rate_limits(self, *, deadline: float | None = None) -> RateLimits:
        return RateLimits(None, None)

    def close(self) -> None:
        pass


class StaticSupervisor:
    def __init__(self, client: IdleCodexClient) -> None:
        self.value = client

    def client(self) -> IdleCodexClient:
        return self.value

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


def text_update(message_id: int, text: str) -> dict[str, object]:
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "message_thread_id": THREAD_ID,
            "is_topic_message": True,
            "from": {"id": OWNER_ID, "is_bot": False},
            "chat": {"id": CHAT_ID, "type": "supergroup", "title": "Example"},
            "text": text,
        },
    }


class HubHarness:
    """A Controller bound to one fictional project root and forum topic."""

    def __init__(
        self,
        base: Path,
        *,
        agents: tuple[AgentDefinition, ...] = (CODEX,),
        dispatch_mode: str = "queue",
    ) -> None:
        self.root = base / "project"
        init_git_root(self.root)
        self.config = HubConfig(
            schema_version=1,
            owner_user_ids=(OWNER_ID,),
            registry_path=base / "projects.json",
            state_path=base / "state.db",
            codex_socket_path=base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(ProjectBinding("example-project", CHAT_ID),),
            agents=agents,
            dispatch_mode=dispatch_mode,
        )
        self.registry = ProjectRegistry(
            1, (base,), (Project("example-project", "Example", "Example", self.root),)
        )
        self.config.registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_roots": [str(base)],
                    "projects": [
                        {
                            "project_id": "example-project",
                            "display_name": "Example",
                            "topic_name": "Example",
                            "root": str(self.root),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.client = IdleCodexClient()
        self.telegram = RecordingTelegram()
        service = ProjectHubService.__new__(ProjectHubService)
        service.config = self.config
        service.registry = self.registry
        service.state = HubState.open(self.config.state_path, codex_permission_profile=None)
        service.agent = agents[0]
        service.telegram = cast(Any, self.telegram)
        service.supervisor = cast(Any, StaticSupervisor(self.client))
        service._codex_client = None
        service.usernames = {agent.agent_id: agent.telegram_username for agent in agents}
        service.external_services = {}
        service._queue_stop = threading.Event()
        service._queue_thread = None
        self.service = service
        self._next_message_id = 1

    def send(self, text: str, *, message_id: int | None = None) -> bool:
        if message_id is None:
            message_id = self._next_message_id
        self._next_message_id = max(self._next_message_id, message_id) + 1
        return self.service.handle_update(text_update(message_id, text))

    @property
    def last_reply(self) -> str:
        return self.telegram.sent[-1] if self.telegram.sent else ""

    def topic(self) -> TopicRecord:
        topic = self.service.state.find_topic(CHAT_ID, THREAD_ID)
        if topic is None:
            self.send("/menu")
            topic = self.service.state.find_topic(CHAT_ID, THREAD_ID)
        assert topic is not None
        return topic

    def activate(
        self,
        agent: AgentDefinition,
        *,
        provider_session_id: str | None = "provider-session-1",
        writer_mode: str | None = None,
    ) -> SessionRecord:
        topic = self.topic()
        session = self.service.state.activate_agent(
            topic.topic_id, agent.agent_id, agent.default_model, agent.default_effort
        )
        if provider_session_id is not None:
            self.service.state.bind_provider_session(session.session_id, provider_session_id, None)
        if writer_mode is not None:
            self.service.state.set_writer_mode(session.session_id, writer_mode)
        return self.session()

    def session(self) -> SessionRecord:
        session = self.service.state.active_session(self.topic().topic_id)
        assert session is not None
        return session

    def set_external_services(self, services: dict[str, object]) -> None:
        """Install fake direct-provider services used for non-Codex summaries."""
        self.service.external_services = cast(Any, services)

    def with_config(self, **changes: object) -> None:
        self.service.config = replace(self.service.config, **changes)

    def close(self) -> None:
        self.service.close()
