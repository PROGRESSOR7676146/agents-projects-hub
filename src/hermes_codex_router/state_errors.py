"""Dependency-neutral durable-state errors."""


class StateError(RuntimeError):
    pass


class ControlScopeError(StateError):
    """Retained roots cannot establish one exact control scope."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__("execution_scope_" + reason)


class CodexPermissionSelectionChanged(StateError):
    code = "codex_permission_selection_changed"

    def __init__(self) -> None:
        super().__init__("Codex permission selection differs from the session generation")


class ConnectPermissionSelectionChanged(StateError):
    code = "connect_permission_selection_changed"

    def __init__(self) -> None:
        super().__init__(self.code)
