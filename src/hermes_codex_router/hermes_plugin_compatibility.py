"""Passive checks for the independently running Hermes consumer of Hub state."""

from __future__ import annotations

import ast
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PluginCompatibility:
    ok: bool
    detail: str


def _constants(path: Path) -> dict[str, object]:
    if path.stat().st_size > 16384:
        raise ValueError("oversized metadata")
    result: dict[str, object] = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = node.value.value
    return result


def check_plugin_source(
    source: Path, state_schema: int, expected_revision: str | None
) -> PluginCompatibility:
    try:
        package = source / "hermes_codex_router"
        build = _constants(package / "_build_info.py")
        schema = _constants(package / "schema_compatibility.py")
        if (
            not expected_revision
            or build.get("CLEAN_TREE") is not True
            or build.get("GIT_SHA") != expected_revision
        ):
            return PluginCompatibility(False, "plugin_release_mismatch")
        minimum, maximum = (
            schema.get("MIN_SUPPORTED_SCHEMA_VERSION"),
            schema.get("MAX_SUPPORTED_SCHEMA_VERSION"),
        )
        if (
            type(minimum) is not int
            or type(maximum) is not int
            or not minimum <= state_schema <= maximum
        ):
            return PluginCompatibility(False, "plugin_schema_mismatch")
        return PluginCompatibility(True, "matching clean plugin release; state schema supported")
    except (OSError, ValueError, SyntaxError):
        return PluginCompatibility(False, "plugin_metadata_unavailable")


def probe_running_plugin(
    state_path: Path, unit: str, expected_revision: str | None
) -> PluginCompatibility:
    try:
        process = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        pid = process.stdout.strip()
        if process.returncode or not pid.isdigit() or int(pid) <= 0:
            return PluginCompatibility(False, "plugin_runtime_unavailable")
        # Never return, log or execute the environment or foreign package.
        with (Path("/proc") / pid / "environ").open("rb") as stream:
            raw = stream.read(131073)
        if len(raw) > 131072:
            return PluginCompatibility(False, "plugin_environment_unavailable")
        env = dict(item.split(b"=", 1) for item in raw.split(b"\0") if b"=" in item)
        source = Path(env.get(b"HERMES_PROJECT_HUB_SOURCE", b"").decode())
        if not source.is_absolute():
            return PluginCompatibility(False, "plugin_source_unconfigured")
        connection = sqlite3.connect(state_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        finally:
            connection.close()
        return check_plugin_source(source, version, expected_revision)
    except (OSError, ValueError, subprocess.TimeoutExpired, sqlite3.Error):
        return PluginCompatibility(False, "plugin_compatibility_unavailable")
