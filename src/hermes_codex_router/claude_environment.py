"""Compact capability orientation from the actual prepared Claude launch."""

from __future__ import annotations

from .claude_file_sandbox import FileToolSandboxConfig
from .claude_tool_profiles import profile_tools


def claude_environment_prompt(sandbox: FileToolSandboxConfig | None) -> str:
    lines = ["HUB CLAUDE ENVIRONMENT (orientation, not an additional permission grant)"]
    if sandbox is None:
        lines.append("This launch is text-only. No project file or command tools are enabled.")
        return "\n".join(lines)
    lines.extend(
        (
            f"Profile: {sandbox.profile}. Native tools: {', '.join(sorted(profile_tools(sandbox.profile)))}.",
            f"Bound project: {sandbox.project_root}.",
            "Read the project's AGENTS.md/CLAUDE.md instructions when present. "
            "Preserve existing changes and inspect Git status before changing branches.",
        )
    )
    if sandbox.profile == "editor":
        lines.append(
            "Write/Edit require human permission. Shell, tests and Git commands are unavailable."
        )
        return "\n".join(lines)
    lines.extend(
        (
            "File edits are enabled. Bash requires human Allow once or Bash until restart; "
            "the latter covers later turns of this session/root on this worker until restart. "
            "Deny, expiry and missing permission do not authorize another attempt.",
            "Keep commands in the foreground, including long tests/builds/hooks. "
            "Use a suitable explicit Bash timeout, at most 600000 ms; no background jobs or sandbox bypass.",
            "Run real project tests/builds and required Git hooks. Never disable hooks to make a commit pass. "
            "Linked-worktree shared Git config is readonly: create branches without implicit tracking; "
            "inspect branch, HEAD and files after a partial Git failure, without blind reset/retry.",
            "After a failed command distinguish missing dependencies from denied permissions. "
            "After an unknown outcome inspect actual effects before proposing any retry. "
            "Do not infer that a program is missing just from a failed command.",
        )
    )
    tools = sandbox.toolchain
    if tools is not None:
        lines.append("Configured tool PATH: " + ":".join(map(str, tools.path)))
        if tools.git_python is not None:
            lines.append(
                f"Configured development Python: {tools.git_python}. "
                "Prefer this exact interpreter for project tests and dependency checks; "
                "a short python/python3 alias can select a different environment."
            )
        lines.append(
            "If hub-consult is installed in the configured tools, it accepts a visible question "
            "on stdin and an explicit configured route, e.g. hub-consult astra or hub-consult sol. "
            "It supplies text advice only, without tools, another writer or source-review authority. "
            "Do not read its credential/config or retry failed/unknown calls automatically. "
            "Check program availability rather than assuming every bundle includes it."
        )
    lines.append(
        "Report only observed results; private host state and control credentials are outside this launch."
    )
    return "\n".join(lines)
