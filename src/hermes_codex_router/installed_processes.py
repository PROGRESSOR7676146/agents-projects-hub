"""Passive systemd/process readback for the installed backup contract."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


def _systemctl(*arguments: str) -> str:
    try:
        result = subprocess.run(
            ["systemctl", "--user", *arguments, "--no-pager"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("installed inventory: service manager unavailable") from exc
    return result.stdout


def _incarnation(unit: str) -> tuple[int, str]:
    value = _systemctl("show", unit, "--property=MainPID", "--value").strip()
    if not value.isdecimal() or int(value) < 1:
        raise RuntimeError("installed inventory: active consumer has no process")
    pid = int(value)
    try:
        # comm may contain spaces or parentheses. Fields after its final ')' start at 3.
        fields = (Path("/proc") / value / "stat").read_text().rsplit(")", 1)[1].split()
        started = fields[19]
    except (OSError, IndexError) as exc:
        raise RuntimeError("installed inventory: process incarnation unreadable") from exc
    return pid, started


def observe_units(declared: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Never starts, stops, repairs or executes the observed consumers."""
    try:
        records = json.loads(
            _systemctl("list-units", "--type=service", "--state=active", "--output=json")
        )
        if not isinstance(records, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("unit"), str) for row in records
        ):
            raise ValueError("invalid unit list")
    except (ValueError, TypeError) as exc:
        raise RuntimeError("installed inventory: active consumer list unreadable") from exc
    active = {row["unit"] for row in records}
    selected = {unit for unit in active if unit.startswith("agents-projects-hub")}
    selected.update(active.intersection(declared))
    result: dict[str, dict[str, Any]] = {}
    for unit in sorted(selected):
        before = _incarnation(unit)
        process = Path("/proc") / str(before[0])
        try:
            argv = process.joinpath("cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
            executable = str(process.joinpath("exe").resolve(strict=True))
        except (OSError, UnicodeError) as exc:
            raise RuntimeError("installed inventory: process identity unreadable") from exc
        if _incarnation(unit) != before:
            raise RuntimeError("installed inventory: process changed during readback")
        result[unit] = {
            "argv": argv,
            "executable": executable,
            "start_marker": f"{before[0]}:{before[1]}",
        }
    return result
