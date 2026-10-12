"""Legacy inline Codex invocation and immediate delivery, without Controller coupling."""

from __future__ import annotations

from typing import Callable

from .artifact_delivery import deliver_staged_artifacts_immediately
from .artifacts import create_job_staging
from .codex_appserver import CodexAppServerClient
from .codex_result_lifecycle import (
    InlineCodexTurn,
    post_completion_context,
    post_completion_limits,
    retire_completed_connection,
)
from .hub_config import AgentDefinition, HubConfig
from .metadata import format_telegram_response
from .registry import Project, ProjectRegistry, validate_execution_root
from .state import HubState, SessionRecord, TopicRecord
from .telegram import TelegramBotApi, TopicMessage
from .telegram_activity import telegram_activity
from .telegram_interaction import (
    CODEX_TELEGRAM_CONTRACT_VERSION,
    telegram_developer_instructions,
    telegram_user_turn_prompt,
)
from .telegram_multipart import send_telegram_html_parts
from .terminal import terminal_session_name
from .topic_execution import require_inline_topic


def run_inline_codex_turn(
    *,
    state: HubState,
    config: HubConfig,
    registry: ProjectRegistry,
    agent: AgentDefinition,
    client_factory: Callable[[], CodexAppServerClient],
    telegram_factory: Callable[[], TelegramBotApi],
    project: Project,
    topic: TopicRecord,
    session: SessionRecord,
    text: str,
    message: TopicMessage,
) -> InlineCodexTurn:
    require_inline_topic(state, topic)
    validate_execution_root(registry, project)
    client = client_factory()
    new_session = (
        session.provider_session_id is None
        or state.telegram_contract_version(session.session_id) < CODEX_TELEGRAM_CONTRACT_VERSION
    )
    instructions = telegram_developer_instructions(runtime="codex", new_session=new_session)
    if session.provider_session_id:
        thread = client.resume_thread(
            thread_id=session.provider_session_id,
            cwd=project.root,
            model=session.model,
            developer_instructions=instructions,
        )
    else:
        thread = client.start_thread(
            cwd=project.root,
            model=session.model,
            project_id=project.project_id,
            developer_instructions=instructions,
        )
        session = state.bind_provider_session(
            session.session_id,
            thread.thread_id,
            terminal_session_name(
                project.display_name, topic.title, agent.display_name, topic.thread_id
            ),
        )
    telegram = telegram_factory()
    with telegram_activity(
        telegram,
        chat_id=message.chat_id,
        thread_id=message.thread_id,
        message_id=message.message_id,
    ):
        artifact_job_id, staging_dir = create_job_staging(project.root, prefix="codex-inline")
        turn_id = client.start_turn(
            thread_id=thread.thread_id,
            cwd=project.root,
            text=telegram_user_turn_prompt(text, staging_dir=staging_dir),
            model=session.model,
            effort=session.effort,
        )
        result = client.wait_for_turn(turn_id)
    state.acknowledge_telegram_contract(session.session_id, CODEX_TELEGRAM_CONTRACT_VERSION)
    post_completion_context(state, session.session_id, result)
    response = format_telegram_response(
        result=result,
        agent=agent.display_name,
        model=thread.model,
        effort=session.effort,
        session_label=f"{project.display_name} · {topic.title} · {agent.display_name}",
        limits=post_completion_limits(client),
        timezone_name="Europe/Moscow",
    )
    send_telegram_html_parts(telegram, message.chat_id, message.thread_id, response)
    deliver_staged_artifacts_immediately(
        telegram,
        chat_id=message.chat_id,
        thread_id=message.thread_id,
        project_root=project.root,
        state_path=config.state_path,
        job_id=artifact_job_id,
    )
    return InlineCodexTurn(result.text, client, thread.thread_id, turn_id)


def commit_inline_codex_completion(
    *,
    state: HubState,
    topic: TopicRecord,
    agent_id: str,
    model: str,
    user_text: str,
    context_watermark: int | None,
    dispatch_id: str,
    completed_turn: InlineCodexTurn,
    retire: Callable[[], None],
) -> None:
    if context_watermark is not None:
        state.acknowledge_visible_context(topic.topic_id, agent_id, context_watermark)
    state.record_visible_turn(
        topic.topic_id,
        agent_id=agent_id,
        provider="openai",
        model=model,
        provider_session_id=completed_turn.thread_id,
        user_excerpt=user_text,
        response_excerpt=completed_turn.text,
    )
    state.finish_dispatch(dispatch_id, success=True)
    retire_completed_connection(
        completed_turn.client,
        thread_id=completed_turn.thread_id,
        turn_id=completed_turn.turn_id,
        retire=retire,
        warning=lambda code, detail: state.record_runtime_event("codex", "warning", code, detail),
    )
