"""One test-only pipe owner and fixed source bootstrap; no productive route."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import selectors
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from hermes_codex_router.review_bridge_attempt import BridgeAttemptError, BridgeAttemptGate
from hermes_codex_router.review_bridge_protocol import (
    BridgeFrame,
    BridgeFrameDecoder,
    BridgeFrameError,
    BridgeFrameType,
)
from hermes_codex_router.review_bridge_sequence import (
    BridgeDirection,
    BridgeDisposition,
    BridgeSequence,
    BridgeSequenceError,
)
from hermes_codex_router.review_bridge_write_buffer import (
    BridgeWriteBuffer,
    BridgeWriteError,
    bounded_response_frames,
)
from tests.native_process_capture import owned_fixture_process

_BOOTSTRAP = """
import json, sys, types
sources = json.loads(sys.argv[1])
for name in ('hermes_codex_router', 'tests'):
    module = types.ModuleType(name); module.__path__ = []
    sys.modules[name] = module
for name, source in sources:
    module = types.ModuleType(name); module.__package__ = name.rpartition('.')[0]
    sys.modules[name] = module
    exec(compile(source, '<example-frozen-source>', 'exec'), module.__dict__)
sys.modules['tests.review_bridge_namespace_actor'].main(sources)
"""
_SOURCES = (
    (
        "hermes_codex_router.review_bridge_protocol",
        "src/hermes_codex_router/review_bridge_protocol.py",
    ),
    (
        "hermes_codex_router.review_bridge_sequence",
        "src/hermes_codex_router/review_bridge_sequence.py",
    ),
    (
        "hermes_codex_router.review_bridge_write_buffer",
        "src/hermes_codex_router/review_bridge_write_buffer.py",
    ),
    ("tests.native_process_capture", "tests/native_process_capture.py"),
    ("tests.review_bridge_namespace_actor", "tests/review_bridge_namespace_actor.py"),
)


def actor_argv(executable: str) -> list[str]:
    root = Path(__file__).resolve().parents[1]
    sources = [(name, (root / path).read_text(encoding="utf-8")) for name, path in _SOURCES]
    if any(len(source.encode()) > 40000 for _, source in sources):
        raise ValueError("example_source_bound")
    encoded = json.dumps(sources)
    if len(encoded.encode()) > 120000:
        raise ValueError("example_source_bound")
    return [executable, "-I", "-c", _BOOTSTRAP, encoded]


@dataclass
class PipeFixtureResult:
    success: bool = False
    error: str = ""
    attempted: bool = False
    revoked: bool = False
    request_seen: bool = False
    response_ended: bool = False
    transport_closed: bool = False
    drained: bool = False
    stdout_bytes: int = 0
    stderr: bytes = field(default=b"", repr=False)
    receipt: dict[str, object] = field(default_factory=dict)
    write_calls: int = 0
    short_writes: int = 0
    would_block: int = 0
    elapsed: float = 0
    cleanup_eof: bool = False
    escaped_ready: bool = False


def run_pipe_fixture(
    argv: list[str],
    environment: dict[str, str],
    gate: BridgeAttemptGate,
    *,
    scenario: str = "success",
    timeout: float = 3,
    write_quantum: int = 511,
    pipe_capacity: int = 4096,
    cancel_at: str | None = None,
    pass_fds: tuple[int, ...] = (),
    inputs: dict[str, object] | None = None,
    deny_inherited_stdin: bool = False,
) -> PipeFixtureResult:
    """Finite fake callback, absolute transport deadline and unconditional cleanup."""
    if not 0 < timeout <= 10 or not 1 <= write_quantum <= 8192:
        raise ValueError("example_fixture_budget")
    if cancel_at not in (None, "before_claim", "after_claim", "partial_response"):
        raise ValueError("example_cancel_stage")
    result = PipeFixtureResult()
    sequence, buffer, decoder = BridgeSequence(), BridgeWriteBuffer(), BridgeFrameDecoder()
    pending: deque[BridgeFrame] = deque()
    started = time.monotonic()
    deadline = started + timeout
    output, diagnostics = bytearray(), bytearray()
    response_digest: str | None = None
    response_size = 0
    exit_payload: bytes | None = None
    witness_descriptor = -1
    diagnostic_descriptor = -1
    try:
        with owned_fixture_process(
            argv, environment, stdin=subprocess.PIPE, pass_fds=pass_fds
        ) as process:
            assert (
                process.stdin is not None
                and process.stdout is not None
                and process.stderr is not None
            )
            fixture_inputs = dict(inputs or {})
            if deny_inherited_stdin:
                info = os.fstat(process.stdin.fileno())
                denied = fixture_inputs.get("denied_inodes", [])
                assert isinstance(denied, list)
                fixture_inputs["denied_inodes"] = [*denied, [info.st_dev, info.st_ino]]
            specification = json.dumps(
                {
                    "scenario": scenario,
                    "inputs": fixture_inputs,
                    "fixture_timeout": min(timeout * 0.8, 8),
                }
            ).encode()
            pending.extend(
                (
                    BridgeFrame(BridgeFrameType.SPEC, specification),
                    BridgeFrame(BridgeFrameType.CAPSULE, gate.capsule_bytes),
                )
            )
            if cancel_at == "before_claim":
                gate.cancel()
                pending.append(BridgeFrame(BridgeFrameType.CANCEL, b""))
            witness_descriptor = os.dup(process.stdout.fileno())
            diagnostic_descriptor = os.dup(process.stderr.fileno())
            for stream in (process.stdin, process.stdout, process.stderr):
                os.set_blocking(stream.fileno(), False)
            fcntl.fcntl(process.stdin, fcntl.F_SETPIPE_SZ, pipe_capacity)
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        result.error = "example_pipe_deadline"
                        break
                    while pending:
                        if not buffer.enqueue(pending[0]):
                            break
                        sequence.observe(BridgeDirection.HOST_TO_CHILD, pending.popleft())
                    if not process.stdin.closed:
                        writing = buffer.observation.pending_bytes > 0
                        try:
                            selector.get_key(process.stdin)
                            registered = True
                        except KeyError:
                            registered = False
                        if writing and not registered:
                            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                        elif not writing and registered:
                            selector.unregister(process.stdin)
                        if (
                            not writing
                            and not pending
                            and (
                                sequence.observation.response_ended
                                or sequence.observation.cancelled
                                or sequence.observation.exit_seen
                            )
                        ):
                            sequence.finish(BridgeDirection.HOST_TO_CHILD)
                            process.stdin.close()
                    for key, _ in selector.select(min(0.05, remaining)):
                        if time.monotonic() >= deadline:
                            result.error = "example_pipe_deadline"
                            break
                        if key.data == "stdin":
                            offer = buffer.peek(max_bytes=write_quantum)
                            try:
                                written = os.write(key.fd, offer)
                            except BlockingIOError:
                                result.would_block += 1
                                buffer.advance(0)
                                continue
                            result.write_calls += 1
                            if 0 < written < len(offer):
                                result.short_writes += 1
                            buffer.advance(written)
                            if (
                                cancel_at == "partial_response"
                                and sequence.observation.request_seen
                                and written
                            ):
                                gate.cancel()
                                buffer.cancel()
                                selector.unregister(process.stdin)
                                process.stdin.close()
                                result.error = "example_partial_abort"
                                break
                            continue
                        try:
                            chunk = os.read(key.fd, 8192)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                            if key.data == "stdout":
                                decoder.finish()
                                sequence.finish(BridgeDirection.CHILD_TO_HOST)
                            continue
                        if key.data == "stderr":
                            if len(diagnostics) + len(chunk) > 16384:
                                result.error = "example_stderr_bound"
                                break
                            diagnostics.extend(chunk)
                            continue
                        for frame in decoder.feed(chunk):
                            disposition = sequence.observe(BridgeDirection.CHILD_TO_HOST, frame)
                            if frame.kind is BridgeFrameType.REQUEST:
                                response = gate.submit(frame)
                                response_digest, response_size = (
                                    hashlib.sha256(response).hexdigest(),
                                    len(response),
                                )
                                if cancel_at == "after_claim":
                                    gate.cancel()
                                    pending.append(BridgeFrame(BridgeFrameType.CANCEL, b""))
                                else:
                                    pending.extend(
                                        bounded_response_frames(b"example-response", response)
                                    )
                            elif (
                                frame.kind is BridgeFrameType.NATIVE_STDOUT
                                and disposition is BridgeDisposition.OBSERVED
                            ):
                                output.extend(frame.payload)
                            elif frame.kind is BridgeFrameType.NATIVE_EXIT:
                                exit_payload = frame.payload
                        if result.error:
                            break
                    if result.error:
                        break
                if not result.error:
                    while (
                        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                        is None
                    ):
                        if time.monotonic() >= deadline:
                            result.error = "example_pipe_deadline"
                            break
                        time.sleep(0.01)
        # Reaping/stream close succeeded before success is even considered.
        if not result.error and process.returncode != 0:
            result.error = "example_actor_exit"
    except (BridgeFrameError, BridgeSequenceError, BridgeWriteError, BridgeAttemptError) as error:
        result.error = str(error)
    except OSError:
        result.error = "example_pipe_io"
    finally:
        result.attempted, result.revoked = gate.observation.attempted, gate.observation.revoked
        gate.close()
        buffer.cancel()
        if witness_descriptor >= 0:
            try:
                cleanup_deadline = time.monotonic() + 1
                drained_bytes = 0
                with selectors.DefaultSelector() as witness:
                    for descriptor, role in (
                        (witness_descriptor, "stdout"),
                        (diagnostic_descriptor, "stderr"),
                    ):
                        if descriptor >= 0:
                            os.set_blocking(descriptor, False)
                            witness.register(descriptor, selectors.EVENT_READ, role)
                    while witness.get_map() and time.monotonic() < cleanup_deadline:
                        for key, _ in witness.select(0.05):
                            try:
                                chunk = os.read(key.fd, 8192)
                            except BlockingIOError:
                                continue
                            if not chunk:
                                witness.unregister(key.fd)
                                if key.data == "stdout":
                                    result.cleanup_eof = True
                            elif key.data == "stderr":
                                if len(diagnostics) + len(chunk) > 16384:
                                    result.error = result.error or "example_stderr_bound"
                                    witness.unregister(key.fd)
                                else:
                                    diagnostics.extend(chunk)
                            else:
                                drained_bytes += len(chunk)
                                if drained_bytes > 256 * 1024:
                                    witness.unregister(key.fd)
            finally:
                os.close(witness_descriptor)
                if diagnostic_descriptor >= 0:
                    os.close(diagnostic_descriptor)
    observation = sequence.observation
    result.request_seen, result.response_ended = (
        observation.request_seen,
        observation.response_ended,
    )
    result.transport_closed = observation.transport_closed
    result.drained = (
        not pending and buffer.observation.admitted_bytes == buffer.observation.advanced_bytes
    )
    result.stdout_bytes, result.stderr = len(output), bytes(diagnostics)
    result.escaped_ready = bytes(output) == b"example-escaped-ready"
    result.elapsed = time.monotonic() - started
    if not result.error and cancel_at is None:
        try:
            receipt = json.loads(output)
            if (
                not isinstance(receipt, dict)
                or set(receipt) != {"response_sha256", "response_size", "isolated"}
                or receipt["response_sha256"] != response_digest
                or type(receipt["response_size"]) is not int
                or receipt["response_size"] != response_size
                or type(receipt["isolated"]) is not bool
            ):
                raise ValueError
            result.receipt = receipt
        except (ValueError, UnicodeError):
            result.error = "example_receipt_invalid"
        result.success = (
            not result.error
            and result.drained
            and result.transport_closed
            and result.response_ended
            and result.cleanup_eof
            and exit_payload == b"0"
        )
        if not result.success and not result.error:
            result.error = "example_completion_incomplete"
    return result
