from __future__ import annotations

from .telegram import TelegramError


def delivery_retry_delay(error: BaseException, attempt_count: int) -> int:
    """Persist this delay, rather than sleeping or consuming cooldown attempts."""
    backoff = min(300, 2 ** min(9, max(0, attempt_count - 1)))
    server_minimum = error.retry_after if isinstance(error, TelegramError) else None
    return max(backoff, server_minimum or 0)
