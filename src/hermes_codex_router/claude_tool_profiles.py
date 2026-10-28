"""Tool sets selected by trusted local configuration, never by chat or hooks."""

FILE_TOOLS = frozenset({"Read", "Glob", "Grep", "Write", "Edit"})
# Profiles with native Bash, writable project Git metadata and an optional toolchain.
BASH_PROFILES = frozenset({"developer", "workstation"})


def profile_tools(profile: str) -> frozenset[str]:
    if profile == "editor":
        return FILE_TOOLS
    if profile in BASH_PROFILES:
        return FILE_TOOLS | {"Bash"}
    raise ValueError("unsupported Claude tool profile")
