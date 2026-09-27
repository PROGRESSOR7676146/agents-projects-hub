"""Bounded process diagnostics for failures a component deliberately survives.

A record carries only the exception class and a static site label. Exception
text, tracebacks, arguments and paths are never written because provider and
Telegram errors can embed tokens, URLs, prompts or local paths (REQ-OPS-009,
REQ-SEC-004, AC-NF-001). Durable health and user-visible notices remain the
authoritative failure channels; this log only keeps survived failures from
disappearing without trace.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
from typing import TextIO

LOGGER_NAME = "hermes_codex_router"
REPEAT_INTERVAL_SECONDS = 60.0
_SITE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+")
_INVALID_SITE = "invalid_site.unknown"

_logger = logging.getLogger(LOGGER_NAME)
# A library stays silent unless the running process opts in.
_logger.addHandler(logging.NullHandler())
_lock = threading.Lock()
_last_emitted: dict[str, float] = {}
_repeats: dict[str, int] = {}


class DiagnosticHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Marker type so process configuration stays idempotent."""


def configure_process_logging(stream: TextIO | None = None) -> None:
    """Send package diagnostics to stderr (journald under systemd) once."""
    if any(isinstance(handler, DiagnosticHandler) for handler in _logger.handlers):
        return
    handler = DiagnosticHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def survived(site: str, error: BaseException) -> None:
    """Record that a best-effort step failed and execution deliberately continues.

    ``site`` must be a static ``module.step`` label; anything else is replaced
    so dynamic identifiers cannot reach the log. Repeats of the same site and
    class are emitted at most once per interval with a count.
    """
    label = site if len(site) <= 96 and _SITE.fullmatch(site) else _INVALID_SITE
    kind = type(error).__name__[:64]
    key = f"{label}:{kind}"
    current = time.monotonic()
    with _lock:
        last = _last_emitted.get(key)
        if last is not None and current - last < REPEAT_INTERVAL_SECONDS:
            _repeats[key] = _repeats.get(key, 0) + 1
            return
        repeated = _repeats.pop(key, 0)
        _last_emitted[key] = current
    suffix = f" (+{repeated} similar)" if repeated else ""
    _logger.warning("survived %s at %s%s", kind, label, suffix)


def reset_repeat_state() -> None:
    """Forget repeat throttling; for tests only."""
    with _lock:
        _last_emitted.clear()
        _repeats.clear()
