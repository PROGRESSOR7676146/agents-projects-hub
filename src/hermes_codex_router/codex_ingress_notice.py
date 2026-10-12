"""Optional visible explanation from immutable ingress reservation provenance."""

from __future__ import annotations

from collections.abc import Callable

from .codex_ingress_control import INGRESS_PRECAUTION_NOTICE
from .diagnostic_log import survived
from .state import MAX_PROVIDER_RESPONSE_LENGTH, HubState


def ingress_precaution_explanation(state: HubState, job_id: str) -> str:
    try:
        if state.codex_ingress_control.read_cause(job_id) is not None:
            return "\n\n" + INGRESS_PRECAUTION_NOTICE
    except Exception as error:
        survived("codex_ingress_control.explanation_unavailable", error)
    return ""


def append_ingress_precaution(
    state: HubState,
    job_id: str,
    text: str,
    *,
    render: Callable[[str], str] | None = None,
) -> str:
    """Optional copy cannot displace mandatory text or break delivery bounds."""
    candidate = text + ingress_precaution_explanation(state, job_id)
    if len(candidate) > MAX_PROVIDER_RESPONSE_LENGTH or (
        render is not None and len(render(candidate)) > MAX_PROVIDER_RESPONSE_LENGTH
    ):
        return text
    return candidate


def prepare_ingress_notice(state: HubState, job_id: str) -> None:
    try:
        state.codex_ingress_control.prepare_notice(job_id)
    except Exception as error:
        survived("codex_ingress_control.notice_unavailable", error)
