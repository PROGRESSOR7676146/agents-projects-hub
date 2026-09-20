from __future__ import annotations


def quota_window_label(
    duration_minutes: int | None,
    *,
    slot: str,
    compact: bool,
) -> str:
    """Return a truthful label without inferring duration from primary/secondary."""
    if duration_minutes is None or duration_minutes <= 0:
        return f"{slot.title()} window"
    if duration_minutes == 10_080:
        return "Week" if compact else "Weekly"
    if compact:
        if duration_minutes % 1_440 == 0:
            return f"{duration_minutes // 1_440}d"
        if duration_minutes % 60 == 0:
            return f"{duration_minutes // 60}h"
        return f"{duration_minutes}m"
    if duration_minutes % 1_440 == 0:
        return f"{duration_minutes // 1_440}-day"
    if duration_minutes % 60 == 0:
        return f"{duration_minutes // 60}-hour"
    return f"{duration_minutes}-minute"
