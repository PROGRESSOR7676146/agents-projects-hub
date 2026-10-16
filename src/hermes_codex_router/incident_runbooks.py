"""Shipped deterministic incident guidance, never executable control payloads."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

RUNTIME_COMPONENTS = ("controller", "monitor", "sender", "provider_worker", "runtime")
RUNTIME_FAILURE_STATUSES = ("unknown", "degraded", "stale")


@dataclass(frozen=True, slots=True)
class RunbookOption:
    option_id: str
    label: str
    consequence: str


@dataclass(frozen=True, slots=True)
class IncidentRunbook:
    title: str
    options: tuple[RunbookOption, ...]
    recommended_option: str


_INSPECT = RunbookOption(
    "inspect",
    "Inspect local diagnostics",
    "Read cached status, doctor and service logs; execution and permissions stay unchanged.",
)
_WAIT = RunbookOption(
    "wait",
    "Wait for a fresh passive observation",
    "An ordinary transient fault may recover; waiting does not prove health or release a hold.",
)
_AUDIT = RunbookOption(
    "audit",
    "Inspect the exact saved outcome",
    "Use the local indeterminate audit and saved native proof; keep the root held while unknown.",
)
_DELIVERY = RunbookOption(
    "delivery",
    "Inspect saved delivery evidence",
    "Read receipts, send fences and retry deadlines; do not resend an unknown attempt.",
)
_RESTORE = RunbookOption(
    "restore",
    "Prepare a separately approved recovery",
    "Identify exact artifacts, backup and rollback first; inspection grants no restart or activation.",
)


def _runbook(
    title: str, first: RunbookOption = _INSPECT, second: RunbookOption = _WAIT
) -> IncidentRunbook:
    return IncidentRunbook(title, (first, second), first.option_id)


_ENTRIES = {
    "deployment_unhealthy": _runbook("Local diagnostics are unhealthy", second=_RESTORE),
    "deployment_revision_mixed": _runbook(
        "Required components have mixed revisions", second=_RESTORE
    ),
    "deployment_revision_unknown": _runbook(
        "Required release identity is unknown", second=_RESTORE
    ),
    "codex_config_proxy_unavailable": _runbook("Configured local Codex proxy is unavailable"),
    "hermes_recovery_unavailable": _runbook("Independent Hermes channel is unavailable"),
    "tlive_recovery_unavailable": _runbook("Independent remote-approval channel is unavailable"),
    "recovery_plane_unavailable": _runbook(
        "Both independent recovery channels are unavailable", second=_RESTORE
    ),
    "telegram_bot_group_unavailable": _runbook("A configured bot cannot access a project group"),
    "hermes_group_policy_incomplete": _runbook("Hermes group policy is incomplete"),
    "hermes_gateway_heartbeat_stale": _runbook("Hermes event-loop heartbeat is stale"),
    "hermes_telegram_api_unavailable": _runbook("Hermes Telegram API check failed"),
    "hermes_telegram_updates_pending": _runbook("Hermes has pending Telegram updates"),
    "provider_catalog_stale": _runbook("A provider catalogue refresh failed"),
    "dispatch_stuck": _runbook("A dispatch has exceeded its expected interval", _AUDIT, _INSPECT),
    "provider_work_stalled": _runbook("Provider work is stalled", _AUDIT, _INSPECT),
    "unresolved_provider_outcome": _runbook(
        "Provider execution outcome remains unknown", _AUDIT, _INSPECT
    ),
    "telegram_delivery_age_exceeded": _runbook("Final delivery is delayed", _DELIVERY, _INSPECT),
    "progress_delivery_age_exceeded": _runbook("Progress delivery is delayed", _DELIVERY, _INSPECT),
    "unknown_notice_delivery": _runbook(
        "Service notice delivery is unconfirmed", _DELIVERY, _INSPECT
    ),
    "unknown_delivery": _runbook("Final delivery outcome is unknown", _DELIVERY, _INSPECT),
    "unknown_progress_delivery": _runbook(
        "Progress delivery outcome is unknown", _DELIVERY, _INSPECT
    ),
    # Retained local diagnostic; automatic context-size advice remains disabled.
    "codex_context_bloat": _runbook("Explicit local context-size diagnostic"),
}
for _component in RUNTIME_COMPONENTS:
    for _status in RUNTIME_FAILURE_STATUSES:
        _ENTRIES[f"{_component}_health_{_status}"] = _runbook(
            f"Configured {_component.replace('_', ' ')} health is {_status}"
        )

RUNBOOKS = MappingProxyType(_ENTRIES)
_UNKNOWN = _runbook("Unrecognized historical operational alert", second=_RESTORE)


def incident_runbook(code: str) -> IncidentRunbook:
    """Unknown historical codes receive fixed guidance without reflecting input."""
    return RUNBOOKS.get(code, _UNKNOWN)
