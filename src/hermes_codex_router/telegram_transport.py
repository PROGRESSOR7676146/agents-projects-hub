"""Witness the initial urllib connect boundary, before any HTTP send in an attempt."""

from __future__ import annotations

import http.client
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol, cast

# A TCP connect normally takes well under a second; some hosts stall about one
# connect in ten. A stalled connect gets its own short limit instead of the whole
# request budget, and only a proven unsent attempt is repeated.
CONNECT_TIMEOUT_SECONDS = 3.0
CONNECT_ATTEMPTS = 3


class _InitialConnectNotSent(OSError):
    def __init__(self, error: OSError) -> None:
        super().__init__("initial connection failed before HTTP transmission")
        self.error = error


@dataclass
class _Attempt:
    http_started: bool = False


class _SocketFactory(Protocol):
    # Runtime http.client hook absent from typeshed; exercised on supported Python versions.
    _create_connection: Callable[..., socket.socket]


def _observe(
    connection: http.client.HTTPConnection, attempt: _Attempt
) -> http.client.HTTPConnection:
    observed = cast(_SocketFactory, connection)
    create_connection = observed._create_connection
    send = connection.send

    def initial_connect(address: Any, timeout: Any = None, *args: Any, **kwargs: Any) -> Any:
        limit = timeout
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            limit = min(timeout, CONNECT_TIMEOUT_SECONDS)
        try:
            sock = create_connection(address, limit, *args, **kwargs)
        except OSError as error:
            if not attempt.http_started:
                raise _InitialConnectNotSent(error) from None
            raise
        if limit is not timeout:
            sock.settimeout(timeout)  # TLS, send and read keep the request's own timeout.
        return sock

    def observed_send(data: Any) -> None:
        # HTTPConnection.send normally connects lazily. Observe socket creation
        # before marking transmission; a proxy CONNECT also passes through here.
        if connection.sock is None and connection.auto_open:
            connection.connect()
        attempt.http_started = True
        send(data)

    observed._create_connection = initial_connect
    connection.send = observed_send
    return connection


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, attempt: _Attempt) -> None:
        super().__init__()
        self.attempt = attempt

    def http_open(self, req: urllib.request.Request) -> Any:
        def connection(*args: Any, **kwargs: Any) -> http.client.HTTPConnection:
            return _observe(http.client.HTTPConnection(*args, **kwargs), self.attempt)

        return self.do_open(connection, req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, attempt: _Attempt) -> None:
        super().__init__()
        self.attempt = attempt

    def https_open(self, req: urllib.request.Request) -> Any:
        def connection(*args: Any, **kwargs: Any) -> http.client.HTTPConnection:
            return _observe(http.client.HTTPSConnection(*args, **kwargs), self.attempt)

        return self.do_open(connection, req, context=getattr(self, "_context", None))


def _open_once(request: urllib.request.Request, *, timeout: float) -> Any:
    # One fresh witness covers all redirects/proxy steps in this call. Never
    # reuse a process-global opener, attempt state or previously connected socket.
    attempt = _Attempt()
    opener = urllib.request.build_opener(_HTTPHandler(attempt), _HTTPSHandler(attempt))
    return opener.open(request, timeout=timeout)


def urlopen_with_send_proof(request: urllib.request.Request, *, timeout: float) -> Any:
    """Repeat only attempts proven to have failed before any HTTP transmission."""
    for remaining in range(CONNECT_ATTEMPTS - 1, -1, -1):
        try:
            return _open_once(request, timeout=timeout)
        except urllib.error.URLError as error:
            if not remaining or not isinstance(error.reason, _InitialConnectNotSent):
                raise
    raise AssertionError("unreachable")
