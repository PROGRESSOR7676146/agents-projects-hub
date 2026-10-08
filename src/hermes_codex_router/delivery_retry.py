from __future__ import annotations

from .telegram import TelegramError


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
