from __future__ import annotations

import html
import time
from dataclasses import dataclass
from typing import cast

from .codex_accounts import CodexPoolStatus
from .codex_appserver import LimitWindow, RateLimits
from .hub_config import HubConfig
from .model_selection import ModelSelectionError
from .provider_catalog_cache import CatalogSnapshot
from .provider_limits import ProviderLimit, decode_provider_limit
from .provider_telemetry import load_antigravity_telemetry
from .session_controls import bind_controls
from .state import HubState, StateError, TopicRecord
from .status_view import cached_codex_rate_limits, format_accounts, format_session_status


@dataclass(frozen=True, slots=True)
class TextCommandDecision:
    text: str
    response_agent_id: str | None = None


@dataclass(frozen=True, slots=True)
class HtmlCommandDecision:
    html: str
    reply_markup: dict[str, object]


class ControllerCommandOrchestrator:
    """Deterministic command projection over an already-open Controller state."""

    MODEL_PAGE_SIZE = 8

    def __init__(self, config: HubConfig, state: HubState) -> None:
        self.config = config
        self.state = state

    @staticmethod
    def _inline_grid(values: list[tuple[str, str]], width: int = 2) -> dict[str, object]:
        rows: list[list[dict[str, str]]] = []
        for position in range(0, len(values), width):
            rows.append(
                [
                    {
                        "text": label,
                        "callback_data": callback,
                        **({"style": "success"} if label.startswith("✓ ") else {}),
                    }
                    for label, callback in values[position : position + width]
                ]
            )
        return {"inline_keyboard": rows}

    def status(
        self,
        topic: TopicRecord,
        codex_pool: CodexPoolStatus | None,
        codex_rate_limits: RateLimits | None,
    ) -> TextCommandDecision:
        active = self.state.active_session(topic.topic_id)
        if active is None:
            return TextCommandDecision("No active agent session has been created yet.")

        agent = self.config.require_agent(active.agent_id)
        current_account = (
            next((item for item in codex_pool.accounts if item.active), None)
            if agent.runtime == "codex" and codex_pool and codex_pool.available
            else None
        )
        limits = codex_rate_limits or cached_codex_rate_limits(current_account)
        status_model = active.model
        status_effort = active.effort
        status_context = active.context_remaining_percent
        status_account = current_account.identity_hint if current_account else None
        worker_health = next(
            (
                item
                for item in self.state.list_runtime_health()
                if item.component == "provider_worker" and item.agent_id == agent.agent_id
            ),
            None,
        )
        telemetry_settings = self.config.provider_telemetry.get(active.agent_id)
        if telemetry_settings is not None and agent.runtime == "antigravity":
            telemetry = load_antigravity_telemetry(
                telemetry_settings,
                selected_model=active.model,
                selected_effort=active.effort,
            )
            if active.model == "provider-selected" and telemetry.model:
                status_model = telemetry.model
            if active.effort == "default" and telemetry.effort:
                status_effort = telemetry.effort
            if status_context is None:
                status_context = telemetry.context_remaining
            status_account = telemetry.account_hint
            if telemetry.quota_remaining is not None:
                limits = RateLimits(
                    LimitWindow(
                        telemetry.quota_remaining,
                        telemetry.quota_resets_at,
                        None,
                    ),
                    None,
                )
        detail = format_session_status(
            agent=agent.display_name,
            model=status_model,
            effort=status_effort,
            writer=active.writer_mode,
            context_remaining=status_context,
            account_hint=status_account,
            limits=limits,
            timezone_name="Europe/Moscow",
            limits_stale=current_account.quota_stale if current_account else False,
            provider_state=(worker_health.provider_state if worker_health is not None else None),
            provider_error_code=(worker_health.error_code if worker_health is not None else None),
        )
        return TextCommandDecision(detail, response_agent_id=active.agent_id)

    def accounts(
        self,
        codex_pool: CodexPoolStatus | None,
        *,
        now: float | None = None,
    ) -> TextCommandDecision:
        observed_at = time.time() if now is None else now
        pool = codex_pool or CodexPoolStatus(False, False, (), None, 0, "not_configured")
        include_opencode = any(item.runtime == "opencode" for item in self.config.agents)
        event = self.state.latest_runtime_event("opencode", "provider_limit")
        opencode_limit = decode_provider_limit(str(event["detail"])) if event else None
        if opencode_limit is not None and opencode_limit.resets_at <= observed_at:
            opencode_limit = None

        provider_limits: dict[str, ProviderLimit] = {}
        provider_current_accounts: dict[str, str] = {}
        worker_health = {
            item.agent_id: item
            for item in self.state.list_runtime_health()
            if item.component == "provider_worker" and item.agent_id is not None
        }
        for agent_id in self.config.provider_account_hints:
            limit_event = self.state.latest_runtime_event(agent_id, "provider_limit")
            if limit_event is None:
                continue
            limit = decode_provider_limit(str(limit_event["detail"]))
            if limit is not None and limit.resets_at > observed_at:
                provider_limits[agent_id] = limit
        for agent_id, telemetry_settings in self.config.provider_telemetry.items():
            agent = self.config.require_agent(agent_id)
            telemetry = load_antigravity_telemetry(
                telemetry_settings,
                selected_model=agent.default_model,
                selected_effort=agent.default_effort,
            )
            if telemetry.account_hint:
                provider_current_accounts[agent_id] = telemetry.account_hint
            if telemetry.quota_remaining is not None and telemetry.quota_resets_at is not None:
                provider_limits[agent_id] = ProviderLimit(
                    provider=agent_id,
                    window="model",
                    remaining_percent=telemetry.quota_remaining,
                    resets_at=telemetry.quota_resets_at,
                )
        detail = format_accounts(
            pool,
            include_opencode_go=include_opencode,
            opencode_limit=opencode_limit,
            provider_account_hints=self.config.provider_account_hints,
            provider_limits=provider_limits,
            provider_current_accounts=provider_current_accounts,
            provider_states={
                agent_id: item.provider_state for agent_id, item in worker_health.items()
            },
            provider_error_codes={
                agent_id: item.error_code
                for agent_id, item in worker_health.items()
                if item.error_code is not None
            },
        )
        return TextCommandDecision(detail or "No provider accounts are configured.")

    def provider_menu(self, topic: TopicRecord) -> HtmlCommandDecision:
        active = self.state.active_session(topic.topic_id)
        values = []
        for candidate in self.config.agents:
            marker = "✓ " if active and active.agent_id == candidate.agent_id else ""
            values.append((f"{marker}{candidate.display_name}", f"provider:{candidate.agent_id}"))
        return HtmlCommandDecision(
            "Provider → model → effort",
            self._inline_grid(bind_controls(self.state, topic.topic_id, values)),
        )

    @staticmethod
    def _require_catalog(agent_id: str, catalog: CatalogSnapshot) -> None:
        if catalog.agent_id != agent_id:
            raise ModelSelectionError("provider selection is no longer available")

    def model_menu(
        self,
        topic: TopicRecord,
        agent_id: str,
        catalog: CatalogSnapshot,
        *,
        page: int = 0,
    ) -> HtmlCommandDecision:
        self._require_catalog(agent_id, catalog)
        active = self.state.active_session(topic.topic_id)
        page_count = max(
            1,
            (len(catalog.models) + self.MODEL_PAGE_SIZE - 1) // self.MODEL_PAGE_SIZE,
        )
        if page < 0 or page >= page_count:
            raise ModelSelectionError("model catalog page is unavailable")
        start = page * self.MODEL_PAGE_SIZE
        models = catalog.models[start : start + self.MODEL_PAGE_SIZE]
        values = []
        for model in models:
            marker = (
                "✓ "
                if active and active.agent_id == agent_id and active.model == model.model_id
                else ""
            )
            is_highlighted = (
                model.is_new
                and "🆕" not in model.label
                and not model.label.lower().endswith("(new)")
            )
            new_prefix = "🆕 " if is_highlighted else ""
            values.append(
                (f"{marker}{new_prefix}{model.label}", f"choose:{agent_id}:{model.callback_key}")
            )
        navigation: list[tuple[str, str]] = []
        if page > 0:
            navigation.append(("←", f"models:{agent_id}:{page - 1}"))
        navigation.append(("🔄 Обновить", f"modelrefresh:{agent_id}:{page}"))
        if page + 1 < page_count:
            navigation.append(("→", f"models:{agent_id}:{page + 1}"))
        keyboard = cast(
            list[list[dict[str, str]]],
            self._inline_grid(bind_controls(self.state, topic.topic_id, values))["inline_keyboard"],
        )
        keyboard.extend(
            cast(
                list[list[dict[str, str]]],
                self._inline_grid(bind_controls(self.state, topic.topic_id, navigation))[
                    "inline_keyboard"
                ],
            )
        )
        agent = self.config.require_agent(agent_id)
        cached = " · cached" if catalog.last_failure_at is not None else ""
        return HtmlCommandDecision(
            html.escape(f"{agent.display_name}: choose model · {page + 1}/{page_count}{cached}"),
            {"inline_keyboard": keyboard},
        )

    def effort_menu(
        self,
        topic: TopicRecord,
        agent_id: str,
        callback_key: str,
        catalog: CatalogSnapshot,
    ) -> HtmlCommandDecision:
        self._require_catalog(agent_id, catalog)
        model = next(
            (item for item in catalog.models if item.callback_key == callback_key),
            None,
        )
        if model is None:
            raise ModelSelectionError("model selection is unavailable")
        active = self.state.active_session(topic.topic_id)
        values = []
        for effort in model.efforts:
            marker = (
                "✓ "
                if active
                and active.agent_id == agent_id
                and active.model == model.model_id
                and active.effort == effort
                else ""
            )
            values.append(
                (
                    f"{marker}{effort.title()}",
                    f"use:{agent_id}:{model.callback_key}:{effort}",
                )
            )
        return HtmlCommandDecision(
            html.escape(f"{model.label}: choose effort"),
            self._inline_grid(bind_controls(self.state, topic.topic_id, values)),
        )

    def apply_model_selection(
        self,
        topic: TopicRecord,
        agent_id: str,
        callback_key: str,
        effort: str,
        catalog: CatalogSnapshot,
        *,
        expected_session_id: str | None = None,
    ) -> TextCommandDecision:
        self._require_catalog(agent_id, catalog)
        selected = next(
            (item for item in catalog.models if item.callback_key == callback_key),
            None,
        )
        if selected is None or effort not in selected.efforts:
            raise ModelSelectionError("provider selection is no longer available")

        model = selected.model_id
        active = self.state.active_session(topic.topic_id)
        if (
            expected_session_id is not None
            and (active.session_id if active else "") != expected_session_id
        ):
            raise StateError("active session changed; open controls again")
        agent = self.config.require_agent(agent_id)
        if active is None:
            replacement = self.state.activate_agent(
                topic.topic_id,
                agent_id,
                model,
                effort,
                expected_session_id=(
                    expected_session_id if expected_session_id is not None else ""
                ),
            )
            return TextCommandDecision(
                f"{agent.display_name} · {model} · {effort.title()} will start on the next "
                f"message (generation {replacement.generation})."
            )
        if active.writer_mode != "telegram":
            command = "/release" if active.writer_mode == "terminal" else "/return"
            raise StateError(f"Use {command} before changing provider settings")
        if active.agent_id != agent_id:
            replacement = self.state.activate_agent(
                topic.topic_id,
                agent_id,
                model,
                effort,
                expected_session_id=active.session_id,
            )
            if (replacement.model, replacement.effort) != (model, effort):
                replacement = self.state.replace_active_session(
                    topic.topic_id,
                    model=model,
                    effort=effort,
                    expected_session_id=replacement.session_id,
                )
            return TextCommandDecision(
                f"{agent.display_name} is now active (generation {replacement.generation}). "
                "No prior agent history was injected; use /context when you explicitly want it."
            )
        if (active.model, active.effort) == (model, effort):
            return TextCommandDecision("This provider, model, and effort are already active.")
        replacement = self.state.replace_active_session(
            topic.topic_id,
            model=model,
            effort=effort,
            expected_session_id=active.session_id,
        )
        return TextCommandDecision(
            f"{agent.display_name} · {model} · {effort.title()} will start on the next "
            f"message (generation {replacement.generation})."
        )
