"""Tool sets selected by trusted local configuration, never by chat or hooks."""

FILE_TOOLS = frozenset({"Read", "Glob", "Grep", "Write", "Edit"})


def profile_tools(profile: str) -> frozenset[str]:
    if profile == "editor":
        return FILE_TOOLS
    if profile == "developer":
        return FILE_TOOLS | {"Bash"}
    raise ValueError("unsupported Claude tool profile")
