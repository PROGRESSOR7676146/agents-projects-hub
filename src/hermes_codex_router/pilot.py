from __future__ import annotations

from dataclasses import dataclass

from .hub_config import HubConfig
from .metadata import format_telegram_response
from .registry import load_registry
from .state import HubState
from .supervisor import CodexAppServerSupervisor
from .telegram import TelegramBotApi
from .telegram_interaction import (
    CODEX_TELEGRAM_CONTRACT_VERSION,
    telegram_developer_instructions,
    telegram_user_turn_prompt,
)
from .terminal import terminal_session_name


@dataclass(frozen=True, slots=True)
class PilotResult:
    local_session_id: str
    provider_session_id: str
    telegram_message_id: int
    terminal_name: str


def run_codex_pilot(
    config: HubConfig,
    *,
    project_id: str,
    chat_id: int,
    thread_id: int,
    topic_title: str,
) -> PilotResult:
    binding = config.project_for_chat(chat_id)
    if binding.project_id != project_id:
        raise ValueError("Telegram group is bound to a different project")
    project = load_registry(config.registry_path).require_project(project_id)
    agent = config.require_agent("codex")
    if agent.runtime != "codex" or agent.token_file is None:
        raise ValueError("managed Codex bot is not configured")

    state = HubState.open(config.state_path)
    supervisor = CodexAppServerSupervisor(
        config.state_path.parent / "codex-stdio-placeholder.sock",
        stdio_executable=config.codex_stdio_executable,
    )
    try:
        topic = state.observe_topic(
            project_id=project_id,
            chat_id=chat_id,
            thread_id=thread_id,
            title=topic_title,
        )
        session = state.active_session(topic.topic_id)
        if session is None or session.agent_id != agent.agent_id:
            session = state.activate_agent(
                topic.topic_id,
                agent.agent_id,
                agent.default_model,
                agent.default_effort,
            )

        supervisor.start()
        client = supervisor.client()
        thread = client.start_thread(
            cwd=project.root,
            model=session.model,
            project_id=project.project_id,
            developer_instructions=telegram_developer_instructions(
                runtime="codex", new_session=True
            ),
        )
        tab_name = terminal_session_name(
            project.display_name, topic.title, agent.display_name, topic.thread_id
        )
        session = state.bind_provider_session(session.session_id, thread.thread_id, tab_name)
        turn_id = client.start_turn(
            thread_id=thread.thread_id,
            cwd=project.root,
            text=telegram_user_turn_prompt(
                "Connectivity pilot for Agents Projects Hub. Do not use tools and do not modify "
                "files. Reply briefly that the Codex session for the requested topic "
                f"'{topic.title}' is connected and ready.",
            ),
            model=session.model,
            effort=session.effort,
        )
        result = client.wait_for_turn(turn_id)
        state.acknowledge_telegram_contract(session.session_id, CODEX_TELEGRAM_CONTRACT_VERSION)
        limits = client.read_rate_limits()
        html = format_telegram_response(
            result=result,
            agent=agent.display_name,
            model=thread.model,
            effort=session.effort,
            session_label=f"{project.display_name} · {topic.title} · {agent.display_name}",
            limits=limits,
            timezone_name="Europe/Moscow",
        )
        api = TelegramBotApi(agent.token_file.read_text(encoding="utf-8").strip())
        message_id = api.send_html(chat_id, thread_id, html)
        client.close()
        return PilotResult(session.session_id, thread.thread_id, message_id, tab_name)
    finally:
        supervisor.stop()
        state.close()
