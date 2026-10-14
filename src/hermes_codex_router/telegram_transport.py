"""Witness the initial urllib connect boundary, before any HTTP send in an attempt."""

from __future__ import annotations

import http.client
import socket
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Protocol, cast


class InitialConnectNotSent(OSError):
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

    def initial_connect(*args: Any, **kwargs: Any) -> Any:
        try:
            return create_connection(*args, **kwargs)
        except OSError as error:
            if not attempt.http_started:
                raise InitialConnectNotSent(error) from None
            raise

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


def urlopen_with_send_proof(request: urllib.request.Request, *, timeout: float) -> Any:
    # One fresh witness covers all redirects/proxy steps in this call. Never
    # reuse a process-global opener, attempt state or previously connected socket.
    attempt = _Attempt()
    opener = urllib.request.build_opener(_HTTPHandler(attempt), _HTTPSHandler(attempt))
    return opener.open(request, timeout=timeout)
