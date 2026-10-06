"""Explicit transport-only proof for an owner retry before turn submission."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .codex_failure import CodexPreparationError
from .codex_rpc import RpcError


@dataclass(frozen=True, slots=True)
class PreparationRetryBinding:
    canonical_root: Path
    model_provider: str | None


def preparation_retry_binding(
    error: BaseException, *, root: Path, model_provider: str | None
) -> PreparationRetryBinding | None:
    """A missing turn ID or an exception's display text is never sufficient."""
    if type(error) is not CodexPreparationError:
        return None
    cause = error.__cause__
    if isinstance(cause, (EOFError, ConnectionError, TimeoutError)) or (
        type(cause) is RpcError and str(cause) == "Codex notification buffer exceeded its bound"
    ):
        return PreparationRetryBinding(root.resolve(strict=True), model_provider)
    return None
