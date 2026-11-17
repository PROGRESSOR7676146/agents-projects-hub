from __future__ import annotations

import html
import re
from contextlib import contextmanager
from typing import Iterator

MAX_PARTIAL_TEXT = 10_000


class CodexPreparationError(RuntimeError):
    """Caught setup failure before the productive turn/start call was made."""


class CodexRetryBindingError(CodexPreparationError):
    """A queued saved retry no longer has its exact execution authority."""


class UnsupportedCodexPermissionProfileError(CodexPreparationError):
    """The legacy turn policy cannot safely retain a selected custom profile."""

    def __init__(self) -> None:
        super().__init__("Codex selected an unsupported permission profile")


class CodexPermissionProfileError(CodexPreparationError):
    def __init__(self) -> None:
        super().__init__("Codex permission profile selection could not be verified")


class CodexPermissionPolicyDriftError(RuntimeError):
    """Invocation may have effects; terminality still needs exact reconciliation."""

    def __init__(self) -> None:
        super().__init__("Codex permission selection changed during execution")


@contextmanager
def codex_preparation() -> Iterator[None]:
    try:
        yield
    except CodexPreparationError:
        raise
    except Exception as exc:
        raise CodexPreparationError(str(exc)) from exc


def codex_failure_reason(error: BaseException) -> str:
    """Classify a cause for display; this never proves side-effect safety."""
    if isinstance(error, CodexPermissionPolicyDriftError):
        return "permission_policy_changed"
    message = str(error)[:2000].lower()
    if re.search(r"\b429\b|too many requests|usage limit|rate.?limit", message):
        return "rate_limited"
    if (
        isinstance(error, (EOFError, ConnectionError))
        or "closed" in message
        or "disconnected" in message
    ):
        return "connection_lost"
    if isinstance(error, TimeoutError) or "timed out" in message:
        return "timeout"
    return "provider_error"


def partial_response_block(partial: str) -> str:
    """Collapse explicitly incomplete visible text behind Telegram's expander.

    The caller bounds the retained text; this only escapes and labels it so a
    short notice stays readable on a phone (ADR 0106).
    """
    text = partial.strip()
    if not text:
        return ""
    return (
        "\n\n<b>Частичный ответ (не завершён):</b>\n"
        f"<blockquote expandable>{html.escape(text)}</blockquote>"
    )


def codex_failure_notice(
    error: BaseException,
    *,
    turn_status: str = "unknown",
    held_count: int = 0,
    protective_interrupt_attempted: bool = False,
) -> str:
    """Only fixed causes and explicitly visible assistant text reach Telegram.

    Shape (ADR 0106): what happened, what it means, one action.
    """
    if isinstance(error, CodexRetryBindingError):
        return (
            "Повтор не запущен: Hub не смог подтвердить сохранённую задачу, сессию или права. "
            "Codex не запускался. Проверьте привязку в /status, затем отправьте задачу заново."
        )
    if isinstance(error, CodexPermissionProfileError):
        return (
            "Codex не запущен: Hub не смог проверить настроенный профиль прав. "
            "Ничего не выполнялось и не повторялось. "
            "Нужно проверить профиль на компьютере; другой профиль выбирается через /new."
        )
    if isinstance(error, UnsupportedCodexPermissionProfileError):
        return (
            "Codex не запущен: выбран профиль прав, который Hub не поддерживает. "
            "Ничего не выполнялось и не повторялось. Выберите поддерживаемый профиль через /new."
        )
    if isinstance(error, CodexPreparationError):
        return "Codex не запущен: не удалось подготовить задачу. Ничего не выполнялось."
    reason = getattr(error, "failure_reason", codex_failure_reason(error))
    causes = {
        "permission_policy_changed": "Во время задачи у Codex сменилась настройка прав.",
        "rate_limited": "Codex упёрся в лимит запросов провайдера (429).",
        "connection_lost": "Hub потерял связь с Codex до подтверждения завершения.",
        "timeout": "Hub не дождался от Codex подтверждения завершения.",
    }
    default_cause = (
        "Codex завершил ход с ошибкой провайдера."
        if turn_status == "failed"
        else "Hub перестал видеть ход Codex до его завершения."
    )
    notice = "⚠️ " + causes.get(reason, default_cause)
    if protective_interrupt_attempted:
        notice += " Hub попытался прервать ход; остановка не подтверждена."
    terminal = turn_status in {"failed", "interrupted"}
    if terminal:
        notice += "\nХод остановлен, но не доделан: файлы могли измениться. Автоповтора не будет."
        notice += "\nЧтобы продолжить с проверкой состояния, ответьте retry на это сообщение."
        if held_count:
            notice += f" Запросов на паузе: {held_count}."
    else:
        notice += (
            "\nИтог неизвестен: файлы могли измениться. Автоповтора не будет; "
            "новые запуски в проекте на паузе."
        )
        notice += "\nНичего не повторяйте: Hub попробует сверить итог сам и сообщит."
    partial = getattr(error, "partial_text", "")
    if isinstance(partial, str):
        notice += partial_response_block(partial[:MAX_PARTIAL_TEXT])
    return notice


def owner_resolution_action(job_id: str) -> str:
    """The one supported owner decision for an unresolved uncertain job.

    The local owner command records an immutable resolution without replay and
    releases only the project root (ADR 0074).
    """
    return (
        "Снять паузу без повтора может владелец на компьютере: "
        f"<code>indeterminate-resolve {html.escape(job_id)}</code>"
    )


def uncertain_provider_notice(display_name: str, job_id: str) -> str:
    """Explain an uncertain non-Codex outcome without exposing provider diagnostics."""
    safe_name = html.escape(display_name)
    return (
        f"⚠️ {safe_name} прервался без итогового ответа.\n"
        "Итог неизвестен: файлы могли измениться. Автоповтора не будет; "
        "новые запуски в проекте на паузе до вашего решения.\n" + owner_resolution_action(job_id)
    )
