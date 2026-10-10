"""Bound disposable fixture output and always reap the owned process on failure."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager

MAX_NATIVE_INPUT_BYTES = 1024 * 1024


class NativeCaptureError(RuntimeError):
    """A fixed fixture diagnostic, never a raw native stream."""


@contextmanager
def owned_fixture_process(
    argv: Sequence[str],
    environment: dict[str, str],
    *,
    stdin: int = subprocess.DEVNULL,
    pass_fds: tuple[int, ...] = (),
) -> Iterator[subprocess.Popen[bytes]]:
    """Own pipes/group; callers must not poll, wait or reap the leader.

    Observe completion with waitid(WNOWAIT). The reserved PID protects group
    identity until cleanup. A PID namespace supplies the additional boundary
    for descendants that deliberately escape the owned process group.
    """
    process = subprocess.Popen(
        list(argv),
        env=environment,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
        pass_fds=pass_fds,
    )
    try:
        yield process
    finally:
        try:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


def capture_owned_process(
    argv: Sequence[str],
    environment: dict[str, str],
    *,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
    on_stdout: Callable[[bytes], None] | None = None,
    stdin_data: bytes | None = None,
    stdin_limit: int = MAX_NATIVE_INPUT_BYTES,
) -> tuple[int, bytes]:
    if stdin_data is not None and (
        type(stdin_data) is not bytes
        or type(stdin_limit) is not int
        or not 0 < stdin_limit <= MAX_NATIVE_INPUT_BYTES
        or len(stdin_data) > stdin_limit
    ):
        raise NativeCaptureError("native_fixture_input_bound")
    selector: selectors.BaseSelector | None = None
    input_mode = subprocess.DEVNULL if stdin_data is None else subprocess.PIPE
    with owned_fixture_process(argv, environment, stdin=input_mode) as process:
        try:
            selector = selectors.DefaultSelector()
            output = bytearray()
            counts = [0, 0]
            deadline = time.monotonic() + timeout
            assert process.stdout is not None and process.stderr is not None
            selector.register(process.stdout, selectors.EVENT_READ, 0)
            selector.register(process.stderr, selectors.EVENT_READ, 1)
            offset = 0
            if stdin_data is not None:
                assert process.stdin is not None
                os.set_blocking(process.stdin.fileno(), False)
                if stdin_data:
                    selector.register(process.stdin, selectors.EVENT_WRITE, 2)
                else:
                    process.stdin.close()
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NativeCaptureError("native_fixture_timeout")
                for key, _mask in selector.select(min(0.1, remaining)):
                    if key.data == 2:
                        assert stdin_data is not None and process.stdin is not None
                        try:
                            count = os.write(key.fd, stdin_data[offset : offset + 8192])
                        except (InterruptedError, BlockingIOError):
                            continue
                        except BrokenPipeError:
                            raise NativeCaptureError("native_fixture_input_incomplete") from None
                        if count <= 0:
                            raise NativeCaptureError("native_fixture_input_incomplete")
                        offset += count
                        if offset == len(stdin_data):
                            selector.unregister(process.stdin)
                            process.stdin.close()
                        continue
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    index = key.data
                    counts[index] += len(chunk)
                    if counts[index] > (stdout_limit if index == 0 else stderr_limit):
                        raise NativeCaptureError("native_fixture_output_bound")
                    if index == 0:
                        if on_stdout is None:
                            output.extend(chunk)
                        else:
                            on_stdout(chunk)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NativeCaptureError("native_fixture_timeout")
            # Observe completion without reaping: the leader's reserved PID keeps
            # our process-group identity safe until descendant cleanup is done.
            while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NativeCaptureError("native_fixture_timeout")
                time.sleep(min(0.05, remaining))
        finally:
            if selector is not None:
                selector.close()
    assert process.returncode is not None
    return process.returncode, bytes(output)
