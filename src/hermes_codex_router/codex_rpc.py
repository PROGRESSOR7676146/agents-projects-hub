"""Dependency-neutral Codex protocol failures shared by clients and transports."""


class RpcError(RuntimeError):
    pass


class RpcRejectedError(RpcError):
    """The app-server returned an explicit JSON-RPC rejection."""


class RpcOutboundUnavailableError(RpcError):
    """A recorded stdio terminal prevents admission to its response channel."""

    def __init__(self) -> None:
        super().__init__("Codex stdio response channel unavailable")
