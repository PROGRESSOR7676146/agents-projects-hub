"""Explicit opt-in configuration, independent of transport secrets."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .claude_tool_profiles import BASH_PROFILES, profile_tools
from .namespace_toolchain import NamespaceToolchain, parse_toolchain


@dataclass(frozen=True, slots=True)
class ClaudeFilePermissionsConfig:
    tlive_config: Path
    tlive_home: Path
    provider_home: Path
    runtime_roots: tuple[Path, ...]
    python_executable: Path
    hook_code_root: Path
    bwrap_executable: Path
    private_paths: tuple[Path, ...]
    profile: str = "editor"
    toolchain: NamespaceToolchain | None = None
    visible_roots: tuple[Path, ...] = ()


def parse_claude_file_permissions(raw: Any) -> ClaudeFilePermissionsConfig | None:
    if raw is None:
        return None
    keys = {
        "tlive_config",
        "tlive_home",
        "provider_home",
        "runtime_roots",
        "python_executable",
        "hook_code_root",
        "bwrap_executable",
        "private_paths",
    }
    if (
        not isinstance(raw, dict)
        or not keys <= set(raw)
        or set(raw) - keys - {"profile", "toolchain", "visible_roots"}
    ):
        raise ValueError("claude_file_permissions requires exact explicit mount configuration")
    profile = raw.get("profile", "editor")
    profile_tools(profile)
    if "toolchain" in raw and profile not in BASH_PROFILES:
        raise ValueError("toolchain requires an explicit developer or workstation profile")
    if ("visible_roots" in raw) != (profile == "workstation"):
        raise ValueError(
            "visible_roots is required by, and only valid for, the workstation profile"
        )

    def path(value: Any) -> Path:
        if (
            not isinstance(value, str)
            or not value.startswith("/")
            or ".." in Path(value).parts
            or "\x00" in value
        ):
            raise ValueError("claude_file_permissions paths must be absolute")
        return Path(value)

    def paths(key: str) -> tuple[Path, ...]:
        values = raw[key]
        if not isinstance(values, list) or not 1 <= len(values) <= 32:
            raise ValueError("claude_file_permissions mount lists must be bounded and nonempty")
        result = tuple(path(value) for value in values)
        if len(set(result)) != len(result):
            raise ValueError("claude_file_permissions mount lists must be unique")
        return result

    return ClaudeFilePermissionsConfig(
        path(raw["tlive_config"]),
        path(raw["tlive_home"]),
        path(raw["provider_home"]),
        paths("runtime_roots"),
        path(raw["python_executable"]),
        path(raw["hook_code_root"]),
        path(raw["bwrap_executable"]),
        paths("private_paths"),
        profile,
        parse_toolchain(raw["toolchain"]) if "toolchain" in raw else None,
        paths("visible_roots") if "visible_roots" in raw else (),
    )
