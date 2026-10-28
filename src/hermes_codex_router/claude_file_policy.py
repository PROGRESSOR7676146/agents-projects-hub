"""Fixed native settings and a visible-stream backstop for file tools."""

from __future__ import annotations

import json
import shlex
import unicodedata
from pathlib import Path
from typing import Any, Callable

from .claude_file_sandbox import FileToolSandboxConfig
from .claude_mount_pins import SandboxLaunch
from .claude_native_settings import disabled_builtin_plugins
from .claude_permission_wait import NATIVE_PERMISSION_HOOK_SECONDS
from .claude_stream import ClaudeStreamError
from .claude_tool_profiles import BASH_PROFILES, FILE_TOOLS, profile_tools

FILE_TOOL_NAMES = FILE_TOOLS
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


def file_tool_settings(python: Path, *, profile: str = "editor") -> str:
    tools = profile_tools(profile)
    command = shlex.join([str(python), "-I", "-m", "hermes_codex_router.claude_permission_hook"])
    permissions: dict[str, list[str]] = {
        "allow": [],
        "deny": ["Bash", "Agent", "Task", "WebFetch", "WebSearch"],
    }
    if profile in BASH_PROFILES:
        permissions = {
            "allow": sorted(FILE_TOOLS),
            "ask": ["Bash"],
            "deny": ["Agent", "Task", "WebFetch", "WebSearch"],
        }
    return json.dumps(
        {
            "disableAllHooks": False,
            "enabledPlugins": disabled_builtin_plugins(),
            "permissions": permissions,
            "hooks": {
                "PermissionRequest": [
                    {
                        "matcher": "|".join(sorted(tools)),
                        "hooks": [
                            {
                                "type": "command",
                                "command": command,
                                "timeout": NATIVE_PERMISSION_HOOK_SECONDS,
                            }
                        ],
                    }
                ]
            },
        },
        separators=(",", ":"),
    )


def file_tool_argv(argv: tuple[str, ...], sandbox: FileToolSandboxConfig) -> tuple[str, ...]:
    mutable = list(argv)
    mutable.remove("--safe-mode")
    mutable[mutable.index("--settings") + 1] = file_tool_settings(
        sandbox.python_executable, profile=sandbox.profile
    )
    mutable[mutable.index("--permission-mode") + 1] = "manual"
    mutable[mutable.index("--tools") + 1] = "Read,Glob,Grep,Write,Edit" + (
        ",Bash" if sandbox.profile in BASH_PROFILES else ""
    )
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


def validate_file_tool_input(
    tool: str, tool_input: dict[str, Any], root: Path, *, profile: str = "editor"
) -> None:
    if tool not in profile_tools(profile):
        raise ValueError("unsupported profile tool")
    validate_faithful_input(tool_input)
    if tool == "Bash":
        _validate_bash_input(tool_input)
        return
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


def _validate_bash_input(tool_input: dict[str, Any]) -> None:
    if set(tool_input) - {
        "command",
        "description",
        "timeout",
        "run_in_background",
        "dangerouslyDisableSandbox",
    }:
        raise ValueError("unsupported native command fields")
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > 2000:
        raise ValueError("unsupported native command")
    if "description" in tool_input and (
        not isinstance(tool_input["description"], str) or len(tool_input["description"]) > 1024
    ):
        raise ValueError("unsupported native command description")
    if "timeout" in tool_input and (
        type(tool_input["timeout"]) is not int or not 1 <= tool_input["timeout"] <= 600000
    ):
        raise ValueError("unsupported native command timeout")
    for flag in ("run_in_background", "dangerouslyDisableSandbox"):
        if flag in tool_input and tool_input[flag] is not False:
            raise ValueError("unsupported native command authority")


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


def file_tool_event_policy(profile: str) -> Callable[[dict[str, object]], None]:
    profile_tools(profile)
    if profile == "editor":
        return require_file_tool_event
    active_bash: set[str] = set()

    def check(event: dict[str, object]) -> None:
        checked = event
        if _foreground_bash_heartbeat(event, active_bash):
            # This native progress parent is the executing Bash itself. It is
            # not the assistant/user parent identifying an agent child.
            checked = event | {"parent_tool_use_id": None}
        require_file_tool_event(checked, profile=profile)
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            identity = block.get("id")
            if (
                event.get("type") == "assistant"
                and block.get("type") == "tool_use"
                and block.get("name") == "Bash"
                and isinstance(identity, str)
                and 0 < len(identity) <= 128
            ):
                active_bash.add(identity)
            if event.get("type") == "user" and block.get("type") == "tool_result":
                identity = block.get("tool_use_id")
                if isinstance(identity, str):
                    active_bash.discard(identity)

    return check


def _foreground_bash_heartbeat(event: dict[str, object], active: set[str]) -> bool:
    fields = {
        "type",
        "tool_use_id",
        "tool_name",
        "parent_tool_use_id",
        "elapsed_time_seconds",
        "heartbeat",
        "session_id",
        "uuid",
    }
    parent, progress = event.get("parent_tool_use_id"), event.get("tool_use_id")
    elapsed = event.get("elapsed_time_seconds")
    if not (
        set(event) == fields
        and event.get("type") == "tool_progress"
        and event.get("tool_name") == "Bash"
        and event.get("heartbeat") is True
        and isinstance(parent, str)
        and parent in active
        and isinstance(progress, str)
        and progress.startswith(parent + "-heartbeat-")
        and type(elapsed) is int
        and elapsed >= 0
        and all(
            isinstance(value, str) and 0 < len(value) <= 128
            for value in (event["session_id"], event["uuid"])
        )
    ):
        return False
    suffix = progress[len(parent + "-heartbeat-") :]
    return 0 < len(suffix) <= 16 and suffix.isascii() and suffix.isdecimal()


def require_file_tool_event(event: dict[str, object], *, profile: str = "editor") -> None:
    """Never return tool payload or hidden reasoning to the visible callback."""
    tools_allowed = profile_tools(profile)
    if event.get("parent_tool_use_id") is not None:
        raise ClaudeStreamError("Claude file-tool policy was violated")
    kind, subtype = event.get("type"), event.get("subtype")
    if kind in {
        "control_request",
        "control_response",
    }:
        raise ClaudeStreamError("Claude file-tool policy was violated")
    if kind in {"task_started", "task_progress", "task_notification"} or (
        kind == "system" and isinstance(subtype, str) and subtype.startswith("task_")
    ):
        raise ClaudeStreamError("Claude background or child task events are unsupported")
    if kind == "system" and subtype == "init":
        tools = event.get("tools", [])
        if not isinstance(tools, list) or any(tool not in tools_allowed for tool in tools):
            raise ClaudeStreamError("Claude file-tool policy was violated")
        for field in ("mcp_servers", "plugins", "skills"):
            if event.get(field, []) != []:
                raise ClaudeStreamError("Claude file-tool policy was violated")
        if event.get("permissionMode", "manual") not in {"manual", "default"}:
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
    if kind == "tool_progress" and event.get("tool_name") not in tools_allowed:
        raise ClaudeStreamError("Claude file-tool policy was violated")
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for block in content:
            if (
                isinstance(block, dict)
                and isinstance(block.get("type"), str)
                and block["type"].endswith("tool_use")
                and block.get("name") not in tools_allowed
            ):
                raise ClaudeStreamError("Claude file-tool policy was violated")
    if kind == "stream_event":
        # Partial input deltas cannot attest to a complete exact tool request.
        raise ClaudeStreamError("Claude file-tool partial events are unsupported")
