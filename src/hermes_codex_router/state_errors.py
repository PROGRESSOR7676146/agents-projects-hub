"""Dependency-neutral durable-state errors."""


class StateError(RuntimeError):
    pass


class CodexPermissionSelectionChanged(StateError):
    code = "codex_permission_selection_changed"

    def __init__(self) -> None:
        super().__init__("Codex permission selection differs from the session generation")


class ConnectPermissionSelectionChanged(StateError):
    code = "connect_permission_selection_changed"

    def __init__(self) -> None:
        super().__init__(self.code)
