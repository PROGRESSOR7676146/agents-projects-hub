"""Dependency-neutral Codex protocol failures shared by clients and transports."""


class RpcError(RuntimeError):
    pass


class RpcDeadlineError(RpcError):
    """The local RPC response budget expired; submission certainty is separate."""

    def __init__(self) -> None:
        super().__init__("Codex request deadline exceeded")


class RpcSendDeadlineError(RpcError):
    """Local frame-write initiation expired; native certainty is independent."""

    def __init__(self) -> None:
        super().__init__("Codex send-start deadline exceeded")


class RpcRejectedError(RpcError):
    """The app-server returned an explicit JSON-RPC rejection."""


class RpcOutboundUnavailableError(RpcError):
    """A recorded stdio terminal prevents admission to its response channel."""

    def __init__(self) -> None:
        super().__init__("Codex stdio response channel unavailable")
