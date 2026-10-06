"""Dependency-neutral Codex protocol failures shared by clients and transports."""


class RpcError(RuntimeError):
    pass


class RpcRejectedError(RpcError):
    """The app-server returned an explicit JSON-RPC rejection."""
