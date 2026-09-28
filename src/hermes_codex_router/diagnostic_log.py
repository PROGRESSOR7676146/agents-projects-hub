"""Bounded process diagnostics for failures a component deliberately survives.

A record carries only a registered site label and the name of the nearest
exception class from a closed registry. Exception text, arguments and
tracebacks are never written because
provider and Telegram errors can embed tokens, URLs, prompts or local paths
(REQ-OPS-009, REQ-SEC-004, AC-NF-001). The diagnostic path itself is
best-effort: a failing log stream is dropped silently instead of printing the
standard logging traceback, which would include the exception being survived.
Durable health and user-visible notices remain the authoritative failure
channels; this log only keeps survived failures from disappearing without
trace.
"""

from __future__ import annotations

import builtins
import logging
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
# The only class names a record can carry, as (module, qualified name): every
# exception class of the interpreter, taken once at import, and the classes
# listed here. A record names the nearest registered class of the exception;
# the written text always equals a registry entry, so no class built or
# registered at run time can put a name of its own into the log.
NAMED_ERRORS = frozenset(
    ("builtins", name)
    for name, value in vars(builtins).items()
    if isinstance(value, type) and issubclass(value, BaseException)
) | frozenset(
    {
        ("sqlite3", "Error"),
        ("sqlite3", "DatabaseError"),
        ("sqlite3", "OperationalError"),
        ("sqlite3", "IntegrityError"),
        ("sqlite3", "ProgrammingError"),
        ("sqlite3", "InterfaceError"),
        ("json.decoder", "JSONDecodeError"),
        ("subprocess", "SubprocessError"),
        ("subprocess", "CalledProcessError"),
        ("subprocess", "TimeoutExpired"),
        ("socket", "gaierror"),
        ("ssl", "SSLError"),
        ("http.client", "HTTPException"),
        ("http.client", "RemoteDisconnected"),
        ("urllib.error", "URLError"),
        ("urllib.error", "HTTPError"),
        ("aiohttp.client_exceptions", "ClientError"),
        ("aiohttp.client_exceptions", "ClientConnectionError"),
        ("aiohttp.client_exceptions", "ClientConnectorError"),
        ("aiohttp.client_exceptions", "ClientOSError"),
        ("aiohttp.client_exceptions", "ClientResponseError"),
        ("aiohttp.client_exceptions", "ServerDisconnectedError"),
        ("aiohttp.client_exceptions", "ServerTimeoutError"),
        ("hermes_codex_router.codex_appserver", "CodexTurnError"),
        ("hermes_codex_router.codex_appserver", "RpcError"),
        ("hermes_codex_router.codex_appserver", "RpcRejectedError"),
        ("hermes_codex_router.external_runtime", "ExternalRuntimeError"),
        ("hermes_codex_router.external_runtime", "ProviderLimitError"),
        ("hermes_codex_router.external_runtime", "ProviderUnavailableError"),
        ("hermes_codex_router.outbox_sender", "TelegramOutboxSenderError"),
        ("hermes_codex_router.state", "StateError"),
        ("hermes_codex_router.supervisor", "AppServerError"),
        ("hermes_codex_router.telegram", "TelegramError"),
    }
)
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


def _error_kind(error: BaseException) -> str:
    for cls in type(error).__mro__:
        module = getattr(cls, "__module__", None)
        name = getattr(cls, "__qualname__", None)
        if isinstance(module, str) and isinstance(name, str) and (module, name) in NAMED_ERRORS:
            return name
    return UNNAMED_ERROR


def survived(site: str, error: BaseException) -> None:
    """Record that a best-effort step failed and execution deliberately continues.

    Never raises. ``site`` must be a registered label. The record names the
    nearest class of ``error`` found in ``NAMED_ERRORS``, so a class built or
    registered at run time never contributes its name. Repeats of the same
    site and class are emitted at most once per interval with a count.
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
