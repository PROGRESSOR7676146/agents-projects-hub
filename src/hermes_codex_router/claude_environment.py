"""Compact capability orientation from the actual prepared Claude launch."""

from __future__ import annotations

from .claude_file_sandbox import FileToolSandboxConfig
from .claude_tool_profiles import profile_tools


def claude_environment_prompt(sandbox: FileToolSandboxConfig) -> str:
    lines = [
        "HUB CLAUDE ENVIRONMENT (current orientation; supersedes earlier notes, grants no permissions)"
    ]
    lines.extend(
        (
            f"Profile: {sandbox.profile}. Native tools: {', '.join(sorted(profile_tools(sandbox.profile)))}.",
            f"Bound project: {sandbox.project_root}.",
            "Read the project's AGENTS.md/CLAUDE.md instructions when present. Preserve existing changes.",
        )
    )
    if sandbox.profile == "editor":
        lines.append(
            "Write/Edit require human permission. Shell, tests and Git commands are unavailable."
        )
        return "\n".join(lines)
    if sandbox.profile == "workstation":
        lines.append(
            "File edits and foreground Bash are preauthorized. System trees and these source "
            "trees are readable: "
            + ", ".join(map(str, sandbox.visible_roots))
            + ". Only the bound project is persistently writable. Credential directories and "
            "Hub state are not mounted, and .env files and .hub/work found at launch are "
            "hidden; push, deployment and service changes belong to the owner."
        )
    else:
        lines.append(
            "File edits are enabled. Bash requires human Allow once or Bash until restart; "
            "the latter covers later turns of this session/root on this worker until restart. "
            "Deny, expiry and missing permission do not authorize another attempt."
        )
    lines.extend(
        (
            "Keep commands in the foreground, including long tests/builds/hooks. "
            "Use an explicit Bash timeout within the remaining turn budget, at most 600000 ms; "
            "no background jobs or sandbox bypass.",
            "Run real project tests/builds and required Git hooks. Never disable hooks to make a commit pass. "
            "Inspect Git status before changing branches. Linked-worktree shared Git config is readonly: "
            "create branches without implicit tracking; "
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
                f"Configured Git/hook Python: {tools.git_python}. "
                "This is not a declaration of the project's test environment. "
                "Choose the project's interpreter from its instructions/configuration; "
                "confirm sys.prefix and a required dependency import. "
                "A short python/python3 alias can select a different environment."
            )
        lines.append(
            "If hub-consult is installed in the configured tools, it accepts a visible question "
            "on stdin and an explicit locally configured route. No available route names are declared here. "
            "It supplies text advice only, without tools, another writer or source-review authority. "
            "Do not read its credential/config or retry failed/unknown calls automatically. "
            "Check program availability rather than assuming every bundle includes it."
        )
    lines.append(
        "Report only observed results; private host state and control credentials are outside this launch."
    )
    return "\n".join(lines)
