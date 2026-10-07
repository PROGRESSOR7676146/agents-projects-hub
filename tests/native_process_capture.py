"""Bound disposable fixture output and always reap the owned process on failure."""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from collections.abc import Callable, Sequence


class NativeCaptureError(RuntimeError):
    """A fixed fixture diagnostic, never a raw native stream."""


def capture_owned_process(
    argv: Sequence[str],
    environment: dict[str, str],
    *,
    timeout: float,
    stdout_limit: int,
    stderr_limit: int,
    on_stdout: Callable[[bytes], None] | None = None,
) -> tuple[int, bytes]:
    selector: selectors.BaseSelector | None = None
    process = subprocess.Popen(
        list(argv),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
    )
    try:
        selector = selectors.DefaultSelector()
        output = bytearray()
        counts = [0, 0]
        deadline = time.monotonic() + timeout
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, 0)
        selector.register(process.stderr, selectors.EVENT_READ, 1)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NativeCaptureError("native_fixture_timeout")
            for key, _mask in selector.select(min(0.1, remaining)):
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
        # The new process group is exclusively ours. PID namespace teardown is
        # the additional descendant boundary when used by a native fixture.
        try:
            # No poll/wait above has released the leader's PID. Even an exited
            # leader can have descendants retaining pipes, so always terminate
            # this owned group before the first reap.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        finally:
            if selector is not None:
                selector.close()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
    assert process.returncode is not None
    return process.returncode, bytes(output)
