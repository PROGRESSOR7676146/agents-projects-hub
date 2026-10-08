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
import os
import sys
import threading
import time
from types import ModuleType
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
        "worker_activity.scope_open",
        "worker_activity.accepted_binding",
        "worker_activity.observe",
        "worker_activity.observe_early",
        "worker_activity.scope_retire",
        "worker_activity.activity_retire",
        "alerts.session_meta_read",
        "alerts.thread_metadata_read",
        "claude_recovery.artifact_cleanup",
        "codex_live_control.client_close",
        "codex_live_control.contention",
        "codex_live_control.failure",
        "codex_live_control.interrupt_unconfirmed",
        "codex_live_control.shutdown",
        "codex_live_control.state_close",
        "codex_live_control.steering_failure",
        "codex_recovery.artifact_cleanup",
        "codex_recovery.client_close",
        "codex_permissions.interrupt",
        "codex_result_lifecycle.telemetry",
        "codex_result_lifecycle.context",
        "codex_result_lifecycle.retirement",
        "codex_result_lifecycle.warning",
        "controller_result_publication.artifact_cleanup",
        "controller_result_publication.cleanup_report",
        "external_worker.claude_partial_binding",
        "external_runtime.claude_process_observer",
        "worker_claude_activity.open",
        "worker_claude_activity.retire",
        "outbox_sender.claude_activity",
        "external_worker.client_close",
        "external_worker.failure_notice_record",
        "external_worker.health_publish",
        "external_worker.runtime_event_record",
        "hermes_plugin.menu_clear",
        "monitoring.error_health_publish",
        "outbox_sender.health_publish",
        "outbox_sender.message_draft",
        "outbox_sender.runtime_event_record",
        "project_provisioner.client_disconnect",
        "project_provisioner.disconnect_after_unknown",
        "project_provisioner.health_publish",
        "service.client_close",
        "service.failure_notice_record",
        "service.health_publish",
        "service.outbox_error_record",
        "service.queue_error_record",
        "supervisor.client_close",
        "supervisor.idle_probe",
        "telegram_activity.initial_publish",
        "telegram_activity.message_draft",
        "telegram_activity.refresh",
        "turn_observation.artifact_cleanup",
        "service.artifact_cleanup",
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
        ("hermes_codex_router.codex_rpc", "RpcError"),
        ("hermes_codex_router.codex_rpc", "RpcRejectedError"),
        ("hermes_codex_router.external_runtime", "ExternalRuntimeError"),
        ("hermes_codex_router.external_runtime", "ProviderLimitError"),
        ("hermes_codex_router.external_runtime", "ProviderUnavailableError"),
        ("hermes_codex_router.outbox_sender", "TelegramOutboxSenderError"),
        ("hermes_codex_router.state_errors", "StateError"),
        ("hermes_codex_router.supervisor", "AppServerError"),
        ("hermes_codex_router.telegram", "TelegramError"),
    }
)
# Canonical text for every registered class and site. Lookups accept only
# exact ``str`` values, whose hashing, equality and text cannot be overridden,
# and the logged text is always the registry's own string.
_CANONICAL_SITES = {site: site for site in SITES}
# Registered classes by object identity. A class is found in its module once
# the module has defined it (an exception of a class from a module that was
# never imported cannot exist) and is kept alive here, so its id stays unique
# for the life of the process.
_NAMED_BY_ID: dict[int, str] = {}
_NAMED_CLASSES: list[type] = []
_WANTED_NAMES: dict[str, frozenset[str]] = {
    module: frozenset(name for owner, name in NAMED_ERRORS if owner == module)
    for module in {owner for owner, _ in NAMED_ERRORS}
}
_SEEN_MODULES: dict[str, ModuleType] = {}
_SCANNED_SIZES: dict[str, int] = {}
_OVERFLOW_KEY = "overflow"

_logger = logging.getLogger(LOGGER_NAME)
# A library stays silent unless the running process opts in, and package
# records never reach handlers this module does not control.
_logger.addHandler(logging.NullHandler())
_logger.propagate = False
_lock = threading.Lock()


def _reinitialize_after_fork() -> None:
    # A lock held by another thread at fork time would never be released in
    # the child; logging reinitializes its own locks the same way.
    global _lock
    _lock = threading.Lock()


os.register_at_fork(after_in_child=_reinitialize_after_fork)
_last_emitted: dict[str, float] = {}
_repeats: dict[str, int] = {}
_dropped = 0


class DiagnosticHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Stream handler that drops its own failures without printing anything.

    For a stream backed by a file descriptor, each record is written with one
    ``os.write`` call instead of through the stream's Python buffer, and
    ``flush`` leaves that buffer alone because no record is ever in it. A
    forked child otherwise inherits the buffer's lock in whatever state
    another thread left it, and a record logged in the child, or the flush
    ``logging.shutdown`` runs at its normal exit, would wait forever.
    """

    def _descriptor(self) -> int | None:
        try:
            return int(self.stream.fileno())
        except (AttributeError, OSError, ValueError):
            return None

    def emit(self, record: logging.LogRecord) -> None:
        descriptor = self._descriptor()
        if descriptor is None:
            super().emit(record)
            return
        try:
            data = (self.format(record) + self.terminator).encode("utf-8", "replace")
            while data:
                data = data[os.write(descriptor, data) :]
        except Exception:  # noqa: BLE001 - reported through handleError, never raised
            self.handleError(record)

    def flush(self) -> None:
        if self._descriptor() is None:
            super().flush()

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


# Only the method resolution order of the exception's class is read, through
# the member of ``type`` itself; its name, module and namespace are never
# consulted, so no metaclass, descriptor or namespace key code of that class
# runs while it is named.
_CLASS_MRO = type.__dict__["__mro__"]
_CLASS_QUALNAME = type.__dict__["__qualname__"]


def _resolve_registered_errors() -> None:
    # Namespaces are iterated rather than searched, and only exact str keys
    # are compared, so no key object's own comparison ever runs. A module is
    # scanned again while its namespace grows, which covers a module that was
    # still being imported at the previous scan.
    if len(_SEEN_MODULES) < len(_WANTED_NAMES):
        for key, module in list(sys.modules.items()):
            if type(key) is str and key in _WANTED_NAMES and type(module) is ModuleType:
                _SEEN_MODULES[key] = module
    for key, module in list(_SEEN_MODULES.items()):
        namespace = vars(module)
        wanted = _WANTED_NAMES.get(key)
        if wanted is None or _SCANNED_SIZES.get(key) == len(namespace):
            continue
        _SCANNED_SIZES[key] = len(namespace)
        for attribute, value in list(namespace.items()):
            if type(attribute) is not str or attribute not in wanted:
                continue
            if not (isinstance(value, type) and issubclass(value, BaseException)):
                continue
            # Aliases such as builtins.IOError never replace a class's own name.
            qualname = _CLASS_QUALNAME.__get__(value)
            if type(qualname) is str and qualname == attribute and id(value) not in _NAMED_BY_ID:
                _NAMED_BY_ID[id(value)] = attribute
                _NAMED_CLASSES.append(value)


def _error_kind(error: BaseException) -> str:
    try:
        _resolve_registered_errors()
        for cls in _CLASS_MRO.__get__(type(error)):
            name = _NAMED_BY_ID.get(id(cls))
            if name is not None:
                return name
    except Exception:  # noqa: BLE001 - naming a failure must never fail its caller
        return UNNAMED_ERROR
    return UNNAMED_ERROR


def _site_label(site: object) -> str:
    if type(site) is not str:
        return INVALID_SITE
    return _CANONICAL_SITES.get(site, INVALID_SITE)


def survived(site: str, error: BaseException) -> None:
    """Record that a best-effort step failed and execution deliberately continues.

    Never raises. ``site`` must be a registered label. The record names the
    nearest class of ``error`` found in ``NAMED_ERRORS``, so a class built or
    registered at run time never contributes its name. Repeats of the same
    site and class are emitted at most once per interval with a count.
    """
    label = _site_label(site)
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
