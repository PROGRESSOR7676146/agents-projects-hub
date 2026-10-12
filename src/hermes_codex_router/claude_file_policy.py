"""Fixed native settings and a visible-stream backstop for file tools."""

from __future__ import annotations

import json
import shlex
import unicodedata
from pathlib import Path
from typing import Any

from .claude_file_sandbox import FileToolSandboxConfig
from .claude_mount_pins import SandboxLaunch
from .claude_native_settings import disabled_builtin_plugins
from .claude_stream import ClaudeStreamError

FILE_TOOL_NAMES = frozenset({"Read", "Glob", "Grep", "Write", "Edit"})
# Default_Ignorable_Code_Point ranges that are not already covered by the
# category refusals below. Keep invisible letters/marks out of a rendered grant.
_INVISIBLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x2800, 0x2800),
    (0x3164, 0x3164),
    (0xFFA0, 0xFFA0),
)
PROVIDER_ENVIRONMENT = frozenset(
    {
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "LANG",
        "LC_ALL",
        "TZ",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_AUTOUPDATER",
    }
)


def file_tool_settings(python: Path) -> str:
    command = shlex.join([str(python), "-I", "-m", "hermes_codex_router.claude_permission_hook"])
    return json.dumps(
        {
            "disableAllHooks": False,
            "enabledPlugins": disabled_builtin_plugins(),
            "permissions": {
                "allow": [],
                "deny": ["Bash", "Agent", "Task", "WebFetch", "WebSearch"],
            },
            "hooks": {
                "PermissionRequest": [
                    {
                        "matcher": "Read|Glob|Grep|Write|Edit",
                        "hooks": [{"type": "command", "command": command, "timeout": 130}],
                    }
                ]
            },
        },
        separators=(",", ":"),
    )


def file_tool_argv(argv: tuple[str, ...], sandbox: FileToolSandboxConfig) -> tuple[str, ...]:
    mutable = list(argv)
    mutable.remove("--safe-mode")
    mutable[mutable.index("--settings") + 1] = file_tool_settings(sandbox.python_executable)
    mutable[mutable.index("--permission-mode") + 1] = "manual"
    mutable[mutable.index("--tools") + 1] = "Read,Glob,Grep,Write,Edit"
    mutable[1:1] = ["--setting-sources", ""]
    return tuple(mutable)


def wrap_file_tool_argv(
    argv: tuple[str, ...], environment: dict[str, str], cwd: Path, sandbox: FileToolSandboxConfig
) -> SandboxLaunch:
    isolated = {key: value for key, value in environment.items() if key in PROVIDER_ENVIRONMENT}
    isolated.update(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_AUTOUPDATER="1")
    launch = sandbox.wrap(argv, isolated, cwd)
    launch.environment["HUB_CLAUDE_PERMISSION_SOCKET"] = "/run/hub-permission.sock"
    return launch


def validate_file_tool_input(tool: str, tool_input: dict[str, Any], root: Path) -> None:
    validate_faithful_input(tool_input)
    field = "file_path" if tool in {"Read", "Write", "Edit"} else "path"
    raw = tool_input.get(field, str(root) if field == "path" else None)
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw) > 4096
        or "\x00" in raw
        or raw.startswith("~")
    ):
        raise ValueError("unsupported file-tool target")
    selected = Path(raw)
    if ".." in selected.parts:
        raise ValueError("unsupported file-tool traversal")
    if not selected.is_absolute():
        selected = root / selected
    if selected != root and root not in selected.parents:
        raise ValueError("file-tool lexical target is outside the bound root")
    lexical = selected
    selected = selected.resolve(strict=False)
    if selected != root and root not in selected.parents:
        raise ValueError("file-tool target is outside the bound root")
    if tool in {"Write", "Edit"} and any(
        part.casefold() == ".git"
        for target in (lexical, selected)
        for part in target.relative_to(root).parts
    ):
        raise ValueError("Git metadata is outside the writable file-tool surface")
    if tool == "Glob":
        pattern = tool_input.get("pattern")
        if not isinstance(pattern, str) or pattern.startswith("/") or ".." in Path(pattern).parts:
            raise ValueError("unsupported file-tool pattern")


def validate_faithful_input(tool_input: Any) -> None:
    """Reject data that JSON/Telegram could hide or round before human review."""
    pending = [tool_input]
    visited = 0
    while pending:
        value = pending.pop()
        visited += 1
        if visited > 1024:
            raise ValueError("file-tool input is too complex")
        if isinstance(value, str):
            for char in value:
                code = ord(char)
                if (
                    unicodedata.category(char) in {"Cf", "Co", "Cn", "Cs", "Zl", "Zp"}
                    or unicodedata.category(char) == "Cc"
                    and char not in "\t\n"
                    or unicodedata.category(char) == "Zs"
                    and char != " "
                    or any(low <= code <= high for low, high in _INVISIBLE_RANGES)
                    or 0xFE00 <= code <= 0xFE0F
                    or 0xE0100 <= code <= 0xE01EF
                ):
                    raise ValueError("file-tool input cannot be faithfully previewed")
        elif type(value) is int and abs(value) > 2**53 - 1 or isinstance(value, float):
            raise ValueError("file-tool number cannot be faithfully previewed")
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


def require_file_tool_event(event: dict[str, object]) -> None:
    """Never return tool payload or hidden reasoning to the visible callback."""
    if event.get("parent_tool_use_id") is not None:
        raise ClaudeStreamError("Claude file-tool policy was violated")
    kind, subtype = event.get("type"), event.get("subtype")
    if kind in {
        "control_request",
        "control_response",
        "task_started",
        "task_progress",
        "task_notification",
    }:
        raise ClaudeStreamError("Claude file-tool policy was violated")
    if kind == "system" and subtype == "init":
        tools = event.get("tools", [])
        if not isinstance(tools, list) or any(tool not in FILE_TOOL_NAMES for tool in tools):
            raise ClaudeStreamError("Claude file-tool policy was violated")
        for field in ("mcp_servers", "plugins", "skills"):
            if event.get(field, []) != []:
                raise ClaudeStreamError("Claude file-tool policy was violated")
        if event.get("permissionMode", "manual") not in {"manual", "default"}:
            raise ClaudeStreamError("Claude file-tool policy was violated")
    if kind == "system" and isinstance(subtype, str) and subtype.startswith("task_"):
        raise ClaudeStreamError("Claude file-tool policy was violated")
    if (
        isinstance(kind, str)
        and kind.startswith("hook_")
        or kind == "system"
        and isinstance(subtype, str)
        and subtype.startswith("hook_")
    ):
        if event.get("hook_name") != "PermissionRequest":
            raise ClaudeStreamError("Claude file-tool hook policy was violated")
    if kind == "tool_progress" and event.get("tool_name") not in FILE_TOOL_NAMES:
        raise ClaudeStreamError("Claude file-tool policy was violated")
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and isinstance(block.get("type"), str)
                and block["type"].endswith("tool_use")
                and block.get("name") not in FILE_TOOL_NAMES
            ):
                raise ClaudeStreamError("Claude file-tool policy was violated")
    if kind == "stream_event":
        # Partial input deltas cannot attest to a complete exact tool request.
        raise ClaudeStreamError("Claude file-tool partial events are unsupported")
