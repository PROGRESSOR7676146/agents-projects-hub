"""Russian owner-facing copy for pre-start refusals and limits (ADR 0106).

Each notice says what happened, what it means and one action. The provider's
fixed English refusal stays available as a collapsed technical detail; it never
carries paths, credentials or raw diagnostics (its raise sites guarantee that).
"""

from __future__ import annotations

import html

# Shared meaning: a pre-start refusal proves no provider work began.
_NOTHING_RAN = "Ничего не выполнялось."
_SETUP = "Повтор не поможет, пока настройку на компьютере не исправят."

_NOT_STARTED: dict[str, tuple[str, str]] = {
    "claude_session_local_busy": (
        "сессия открыта в локальном CLI.",
        "Закройте CLI и отправьте /return.",
    ),
    "claude_session_mode_changed": (
        "профиль доступа или хранилище сессии изменились и не совпадают с этой беседой. "
        "Беседа сохранена.",
        "Верните прежнюю настройку или начните новую беседу: /new.",
    ),
    "claude_session_preparation_failed": (
        "не удалось подготовить сохранённую сессию.",
        _SETUP,
    ),
    "claude_permission_host_unverified": (
        "Hub не смог проверить изоляцию и права инструментов.",
        _SETUP,
    ),
    "claude_host_unverified": ("Hub не смог проверить окружение запуска.", _SETUP),
    "claude_cli_capabilities_unverified": (
        "Hub не смог проверить средства изоляции CLI.",
        _SETUP,
    ),
    "claude_cpa_route_unverified": ("маршрут к провайдеру не прошёл проверку.", _SETUP),
    "claude_cpa_credential_ambiguous": ("учётные данные маршрута неоднозначны.", _SETUP),
    "claude_subscription_route_unverified": ("токен подписки не прошёл проверку.", _SETUP),
    "claude_cli_unavailable": ("CLI не запустился.", "Отправьте запрос ещё раз чуть позже."),
    "claude_stream_input_unverified": (
        "не удалось подготовить текст запроса.",
        "Отправьте запрос ещё раз.",
    ),
    "claude_image_input_unverified": (
        "изображение нельзя передать в текущем режиме.",
        "Отправьте запрос без изображения.",
    ),
    "claude_image_configuration_conflict": (
        "изображения несовместимы с включёнными файловыми инструментами.",
        "Отправьте запрос без изображения.",
    ),
    "claude_material_notice_unverified": (
        "вложения не уместились в допустимый объём.",
        "Отправьте меньше вложений за раз.",
    ),
    "claude_resume_invalid": (
        "идентификатор сохранённой сессии неверен.",
        "Начните новую беседу: /new.",
    ),
    "claude_identity_ambiguous": (
        "идентификатор сессии неоднозначен.",
        "Начните новую беседу: /new.",
    ),
    "external_session_identity_unsupported": (
        "этот агент не поддерживает выбранную сессию.",
        "Начните новую беседу: /new.",
    ),
    "claude_effort_unsupported": (
        "выбранный уровень усилий не поддерживается.",
        "Выберите другой уровень усилий.",
    ),
    "unsupported_network_location": (
        "агент недоступен из текущей сети компьютера.",
        "Повторите, когда сеть сменится.",
    ),
    "execution_root_invalid": (
        "папка проекта не прошла проверку.",
        "Нужна проверка регистрации проекта на компьютере.",
    ),
}


def not_started_notice(display_name: str, code: str, detail: str) -> str:
    """Explain a refusal that happened before any provider invocation."""
    reason, action = _NOT_STARTED.get(
        code, ("проверка перед запуском не прошла.", "Отправьте запрос ещё раз.")
    )
    text = f"⛔ {html.escape(display_name)} не запущен: {reason} {_NOTHING_RAN}\n{action}"
    if detail.strip():
        text += f"\n<blockquote expandable>{html.escape(detail.strip())}</blockquote>"
    return text


def limit_notice(display_name: str) -> str:
    """A recorded provider limit; /accounts shows the passive reset telemetry."""
    return (
        f"⛔ {html.escape(display_name)}: достигнут лимит использования. "
        "Время сброса записано. Посмотреть лимиты: /accounts."
    )
