"""Bounded process diagnostics for failures a component deliberately survives.

A record carries only a registered site label and the name of the nearest
exception class defined in code. Exception text, arguments and tracebacks are
never written because
provider and Telegram errors can embed tokens, URLs, prompts or local paths
(REQ-OPS-009, REQ-SEC-004, AC-NF-001). The diagnostic path itself is
best-effort: a failing log stream is dropped silently instead of printing the
standard logging traceback, which would include the exception being survived.
Durable health and user-visible notices remain the authoritative failure
channels; this log only keeps survived failures from disappearing without
trace.
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
MAX_REPEAT_KEYS = 256
INVALID_SITE = "invalid_site.unknown"
UNNAMED_ERROR = "UnnamedError"

# Every production call site, as a static label. A label outside this set is
# logged as INVALID_SITE, so dynamic text can never become a log field.
SITES = frozenset(
    {
        "alerts.session_meta_read",
        "alerts.thread_metadata_read",
        "codex_recovery.artifact_cleanup",
        "codex_recovery.client_close",
        "controller_result_publication.artifact_cleanup",
        "controller_result_publication.cleanup_report",
        "external_worker.client_close",
        "external_worker.context_telemetry",
        "external_worker.failure_notice_record",
        "external_worker.health_publish",
        "external_worker.runtime_event_record",
        "external_worker.steer_client_close",
        "monitoring.error_health_publish",
        "outbox_sender.health_publish",
        "outbox_sender.message_draft",
        "outbox_sender.runtime_event_record",
        "project_provisioner.client_disconnect",
        "project_provisioner.disconnect_after_unknown",
        "project_provisioner.health_publish",
        "service.client_close",
        "service.context_telemetry",
        "service.failure_notice_record",
        "service.health_publish",
        "service.outbox_error_record",
        "service.queue_error_record",
        "telegram_activity.initial_publish",
        "telegram_activity.message_draft",
        "telegram_activity.refresh",
        "turn_observation.artifact_cleanup",
    }
)
_ERROR_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
# Modules whose classes may name a record: the interpreter, this package and
# its declared runtime dependencies. A class elsewhere, or one built at run
# time, is named by its nearest ancestor from this closed set.
_NAMED_ROOTS = frozenset(
    {"builtins", __name__.partition(".")[0], "aiohttp", "telethon"}
) | frozenset(sys.stdlib_module_names)
_OVERFLOW_KEY = "overflow"

_logger = logging.getLogger(LOGGER_NAME)
# A library stays silent unless the running process opts in, and package
# records never reach handlers this module does not control.
_logger.addHandler(logging.NullHandler())
_logger.propagate = False
_lock = threading.Lock()
_last_emitted: dict[str, float] = {}
_repeats: dict[str, int] = {}
_dropped = 0


class DiagnosticHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Stream handler that drops its own failures without printing anything."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - logging API
        _count_dropped()


def _count_dropped() -> None:
    global _dropped
    with _lock:
        _dropped += 1


def dropped_records() -> int:
    """Records lost because the log stream failed; for tests and diagnostics."""
    with _lock:
        return _dropped


def configure_process_logging(stream: TextIO | None = None) -> None:
    """Send package diagnostics to stderr (journald under systemd) once."""
    if any(isinstance(handler, DiagnosticHandler) for handler in _logger.handlers):
        return
    handler = DiagnosticHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)


def _defined_in_code(cls: type) -> bool:
    name = getattr(cls, "__name__", None)
    module_name = getattr(cls, "__module__", None)
    if not isinstance(name, str) or not isinstance(module_name, str):
        return False
    if not _ERROR_NAME.fullmatch(name) or module_name.partition(".")[0] not in _NAMED_ROOTS:
        return False
    # A class built at run time is not its module's attribute under its own
    # name, so a dynamic name that merely looks like an identifier never
    # qualifies. The module dictionary is read without running module hooks.
    module = sys.modules.get(module_name)
    return module is not None and vars(module).get(name) is cls


def _error_kind(error: BaseException) -> str:
    for cls in type(error).__mro__:
        if _defined_in_code(cls):
            return str(cls.__name__)
    return UNNAMED_ERROR


def survived(site: str, error: BaseException) -> None:
    """Record that a best-effort step failed and execution deliberately continues.

    Never raises. ``site`` must be a registered label. The record names the
    nearest class of ``error`` that is defined in code, so a class built at run
    time never contributes its name. Repeats of the same site and class are
    emitted at most once per interval with a count.
    """
    label = site if site in SITES else INVALID_SITE
    kind = _error_kind(error)
    key = f"{label}:{kind}"
    current = time.monotonic()
    with _lock:
        if key not in _last_emitted and len(_last_emitted) >= MAX_REPEAT_KEYS:
            key = _OVERFLOW_KEY
        last = _last_emitted.get(key)
        if last is not None and current - last < REPEAT_INTERVAL_SECONDS:
            _repeats[key] = _repeats.get(key, 0) + 1
            return
        repeated = _repeats.pop(key, 0)
        _last_emitted[key] = current
    suffix = f" (+{repeated} similar)" if repeated else ""
    try:
        _logger.warning("survived %s at %s%s", kind, label, suffix)
    except Exception:  # noqa: BLE001 - the diagnostic path must never fail its caller
        _count_dropped()


def reset_repeat_state() -> None:
    """Forget repeat throttling and drop counts; for tests only."""
    global _dropped
    with _lock:
        _last_emitted.clear()
        _repeats.clear()
        _dropped = 0
