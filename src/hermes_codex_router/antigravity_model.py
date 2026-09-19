from __future__ import annotations


def model_arguments(model: str | None, effort: str | None) -> tuple[str, ...]:
    """Use the same AGY model selection for productive turns and native resume."""
    if not model:
        return ()
    selected = model
    if effort and effort != "default":
        base, separator, suffix = model.rpartition("-")
        if separator and suffix in {"low", "medium", "high"}:
            selected = base
        selected = f"{selected}-{effort}"
    return ("--model", selected)
