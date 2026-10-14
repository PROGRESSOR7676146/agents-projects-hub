from __future__ import annotations

from typing import Literal

from .telegram import TelegramError, TelegramNotSentError


def delivery_failure_phase(
    error: Exception,
) -> Literal["proven_not_sent", "proven_rejected", "unknown"]:
    if isinstance(error, TelegramNotSentError) and error.status_code is None:
        return "proven_not_sent"
    return "proven_rejected" if proven_delivery_rejection(error) else "unknown"


def retryable_delivery_failure(error: Exception) -> bool:
    return delivery_failure_phase(error) != "unknown"


def proven_delivery_rejection(error: Exception) -> bool:
    """Only an explicit transport rejection proves that a begun send is retryable."""
    if not isinstance(error, TelegramError):
        return False
    if error.failure_class == "api_http":
        return error.status_code == 429
    return (
        error.failure_class == "api_rejection"
        and error.status_code is not None
        and 400 <= error.status_code < 500
        and error.status_code != 408
    )


def delivery_retry_delay(error: BaseException, attempt_count: int) -> int:
    """Persist this delay, rather than sleeping or consuming cooldown attempts."""
    backoff = min(300, 2 ** min(9, max(0, attempt_count - 1)))
    server_minimum = error.retry_after if isinstance(error, TelegramError) else None
    return max(backoff, server_minimum or 0)
