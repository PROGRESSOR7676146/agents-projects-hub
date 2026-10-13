"""Dependency-neutral identities for configured external provider-worker slots."""

from __future__ import annotations

MAX_PROVIDER_WORKER_SLOTS = 16


def worker_instance_id(agent_id: str, slot: int) -> str:
    """Format a slot identity; callers retain configuration/admission validation."""
    return f"{agent_id}-worker" if slot == 1 else f"{agent_id}-worker-{slot}"
