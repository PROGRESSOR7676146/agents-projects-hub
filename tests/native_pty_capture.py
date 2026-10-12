"""Test-only PTY ownership; drain/count/discard every terminal byte."""

from __future__ import annotations

import errno
import fcntl
import math
import os
import selectors
import signal
import struct
import subprocess
import sys
import termios
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path


class NativePtyError(RuntimeError):
    """Fixed fixture diagnostics, never terminal or input contents."""


@dataclass(frozen=True, slots=True)
class NativePtyInput:
    data: bytes = field(repr=False)
    ready: Callable[[], bool] = field(repr=False)


@dataclass(frozen=True, slots=True)
class NativePtyResult:
    exit_code: int
    normal_exit: bool
    tty_verified: bool
    input_bytes: int
    output_bytes: int


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    # The leader remains unreaped, reserving the group identity until cleanup.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def capture_owned_pty(
    argv: Sequence[str],
    environment: dict[str, str],
    *,
    timeout: float,
    output_limit: int,
    inputs: tuple[NativePtyInput, ...] = (),
) -> NativePtyResult:
    """A fixture only, requiring an outer namespace for escaping descendants.

    Readiness gates observe independent protocol/session evidence only, must
    return promptly and never inspect terminal output. Each fixed input is sent
    once, without retries. TTY verification precedes exec and does not establish
    native editor readiness, provider acceptance or successful completion.
    """
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= 120
        or type(output_limit) is not int
        or not 0 < output_limit <= 4 * 1024 * 1024
        or isinstance(argv, (str, bytes))
        or not argv
        or len(argv) > 64
        or any(not isinstance(arg, str) or len(arg) > 16384 for arg in argv)
        or environment.get("TERM") != "xterm-256color"
        or not isinstance(inputs, tuple)
        or len(inputs) > 16
        or any(
            not isinstance(step, NativePtyInput)
            or type(step.data) is not bytes
            or not step.data
            or not callable(step.ready)
            for step in inputs
        )
        or sum(len(step.data) for step in inputs) > 65536
    ):
        raise NativePtyError("native_pty_arguments_invalid")
    descriptors: set[int] = set()
    selector = None
    process = None
    try:
        master, slave = os.openpty()
        descriptors.update((master, slave))
        reader, writer = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
        descriptors.update((reader, writer))
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        os.set_blocking(master, False)
        trampoline = Path(__file__).with_name("native_pty_exec.py")
        try:
            process = subprocess.Popen(
                [sys.executable, "-I", str(trampoline), str(writer), *argv],
                env=environment,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                pass_fds=(writer,),
                close_fds=True,
                start_new_session=True,
            )
        except OSError:
            raise NativePtyError("native_pty_launch_failed") from None
        for descriptor in (slave, writer):
            os.close(descriptor)
            descriptors.remove(descriptor)
        selector = selectors.DefaultSelector()
        selector.register(master, selectors.EVENT_READ, "terminal")
        selector.register(reader, selectors.EVENT_READ, "facts")
        deadline = time.monotonic() + timeout
        handshake = bytearray()
        verified = facts_closed = terminal_closed = False
        index = offset = input_bytes = output_bytes = 0
        writable = False
        observed = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NativePtyError("native_pty_timeout")
            if observed is None:
                observed = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if observed is not None:
                    _kill_group(process)
                    if index != len(inputs):
                        raise NativePtyError("native_pty_input_incomplete")
            if observed is not None and facts_closed and terminal_closed:
                if not verified:
                    raise NativePtyError("native_pty_terminal_unproven")
                normal = observed.si_code == os.CLD_EXITED
                return NativePtyResult(
                    observed.si_status if normal else -observed.si_status,
                    normal,
                    verified,
                    input_bytes,
                    output_bytes,
                )
            if verified and index < len(inputs) and not writable and not terminal_closed:
                try:
                    ready = inputs[index].ready()
                except Exception:
                    raise NativePtyError("native_pty_input_gate_failed") from None
                if type(ready) is not bool:
                    raise NativePtyError("native_pty_input_gate_failed")
                if time.monotonic() >= deadline:
                    raise NativePtyError("native_pty_timeout")
                if ready:
                    writable = True
                    selector.modify(
                        master, selectors.EVENT_READ | selectors.EVENT_WRITE, "terminal"
                    )
            for key, mask in selector.select(min(0.05, remaining)):
                if key.data == "facts":
                    chunk = os.read(reader, 64)
                    if chunk:
                        handshake.extend(chunk)
                        if len(handshake) > 5 or not b"PTY1\n".startswith(handshake):
                            raise NativePtyError("native_pty_terminal_unproven")
                    else:
                        facts_closed = True
                        verified = handshake == b"PTY1\n"
                        selector.unregister(reader)
                        os.close(reader)
                        descriptors.remove(reader)
                        if not verified:
                            raise NativePtyError("native_pty_terminal_unproven")
                    continue
                if mask & selectors.EVENT_READ:
                    try:
                        chunk = os.read(master, 8192)
                    except OSError as error:
                        if error.errno != errno.EIO:
                            raise
                        chunk = b""
                    if chunk:
                        output_bytes += len(chunk)
                        if output_bytes > output_limit:
                            raise NativePtyError("native_pty_output_bound")
                    else:
                        terminal_closed = True
                        selector.unregister(master)
                        if index < len(inputs):
                            raise NativePtyError("native_pty_input_incomplete")
                if mask & selectors.EVENT_WRITE and not terminal_closed:
                    if time.monotonic() >= deadline:
                        raise NativePtyError("native_pty_timeout")
                    try:
                        count = os.write(master, inputs[index].data[offset : offset + 4096])
                    except (InterruptedError, BlockingIOError):
                        continue
                    except OSError:
                        raise NativePtyError("native_pty_input_incomplete") from None
                    if count <= 0:
                        raise NativePtyError("native_pty_input_incomplete")
                    input_bytes += count
                    offset += count
                    if offset == len(inputs[index].data):
                        index += 1
                        offset = 0
                        writable = False
                        selector.modify(master, selectors.EVENT_READ, "terminal")
    except NativePtyError:
        raise
    except OSError:
        raise NativePtyError("native_pty_capture_failed") from None
    finally:
        if selector is not None:
            selector.close()
        try:
            if process is not None:
                _kill_group(process)
                process.wait(timeout=5)
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
