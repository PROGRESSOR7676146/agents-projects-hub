"""Fixed fictional pipe/HTTP actor, loaded as a bounded source snapshot."""

from __future__ import annotations

import json
import os
import selectors
import socket
import struct
import sys
import time
from collections import deque

from hermes_codex_router.review_bridge_protocol import (
    BridgeFrame,
    BridgeFrameDecoder,
    BridgeFrameType,
    encode_bridge_frame,
)
from hermes_codex_router.review_bridge_sequence import BridgeDirection, BridgeSequence
from hermes_codex_router.review_bridge_write_buffer import BridgeWriteBuffer
from tests.native_process_capture import owned_fixture_process

# No provider executable, authorization material, endpoint credential or fallback.
_ISOLATION = r"""
import os,pathlib,socket
def verify(inputs,role):
    if not inputs: return False
    denied={tuple(item) for item in inputs['denied_inodes']}
    for target in inputs['hidden']:
        try: pathlib.Path(target).read_bytes()
        except OSError: pass
        else: raise ValueError('example_hidden_path_reachable')
    for process in pathlib.Path('/proc').iterdir():
        if not process.name.isdigit(): continue
        try: descriptors=list(process.joinpath('fd').iterdir())
        except (PermissionError,FileNotFoundError): continue
        for descriptor in descriptors:
            try: info=descriptor.stat()
            except (PermissionError,FileNotFoundError): continue
            if (info.st_dev,info.st_ino) in denied: raise ValueError('example_descriptor_leak')
    for family, endpoint in ((socket.AF_INET,('127.0.0.1',inputs['host_port'])),(socket.AF_UNIX,inputs['pathname']),(socket.AF_UNIX,'\0'+inputs['abstract'])):
        with socket.socket(family) as probe:
            probe.settimeout(.3)
            try: probe.connect(endpoint)
            except OSError: pass
            else: raise ValueError('example_host_endpoint_reachable')
    if os.readlink('/proc/self/ns/net')==inputs['host_netns']: raise ValueError('example_network_shared')
    try: pathlib.Path('.git/new').write_text('forbidden')
    except OSError: pass
    else: raise ValueError('example_project_writable')
    pathlib.Path('/home/example/'+role+'-control').write_text('example-private-session')
    return True
"""
_CLIENT = (
    _ISOLATION
    + r"""
import hashlib,json,selectors,struct,sys,time
raw = sys.stdin.buffer.read(1100000)
size = struct.unpack('>I', raw[:4])[0]
if size > 16384: raise ValueError('example_metadata_bound')
meta = json.loads(raw[4:4+size]); body = raw[4+size:]
if not body or len(body)>1048576: raise ValueError('example_body_bound')
isolated=verify(meta['inputs'],'exec')
scenario=meta['scenario']
line=b'POST /example HTTP/1.1\r\n'
if scenario=='absolute_uri': line=b'POST http://example.com/example HTTP/1.1\r\n'
if scenario=='connect': line=b'CONNECT example.com:443 HTTP/1.1\r\n'
headers=line+b'Host: example.com\r\nContent-Length: '+str(len(body)).encode()+b'\r\n'
if scenario=='duplicate_length': headers+=b'Content-Length: '+str(len(body)).encode()+b'\r\n'
if scenario=='transfer_encoding': headers+=b'Transfer-Encoding: chunked\r\n'
wire=headers+b'\r\n'+body
if scenario=='extra_request': wire+=b'POST /example HTTP/1.1\r\nContent-Length: 0\r\n\r\n'
if scenario=='slow_header': wire=b'POST /example'
if scenario=='slow_body': wire=headers+b'\r\n'+body[:1]
deadline=time.monotonic()+meta['fixture_timeout']
with socket.socket() as channel, selectors.DefaultSelector() as selector:
    channel.setblocking(False)
    channel.connect_ex(('127.0.0.1',meta['port']))
    selector.register(channel,selectors.EVENT_READ|selectors.EVENT_WRITE)
    offset=0; response=bytearray()
    while True:
        if time.monotonic()>=deadline: raise ValueError('example_client_deadline')
        for key,mask in selector.select(.05):
            if mask&selectors.EVENT_WRITE and offset<len(wire):
                try: count=channel.send(wire[offset:offset+257])
                except BlockingIOError: count=0
                offset+=count
                if offset==len(wire): selector.modify(channel,selectors.EVENT_READ)
            if mask&selectors.EVENT_READ:
                try: chunk=channel.recv(4096)
                except BlockingIOError: continue
                if not chunk: break
                response.extend(chunk)
                if len(response)>70000: raise ValueError('example_response_bound')
        else: continue
        break
header,body=bytes(response).split(b'\r\n\r\n',1)
if header!=b'HTTP/1.1 200 OK\r\nContent-Length: '+str(len(body)).encode(): raise ValueError('example_response_header')
digest=hashlib.sha256(body).hexdigest()
if scenario=='bad_receipt': digest='0'*64
print(json.dumps({'response_sha256':digest,'response_size':len(body),'isolated':isolated}),flush=True)
"""
)


def _http_body(raw: bytearray) -> bytes | None:
    position = raw.find(b"\r\n\r\n")
    if position < 0:
        if len(raw) > 8192:
            raise ValueError("example_http_header_bound")
        return None
    if position > 8192:
        raise ValueError("example_http_header_bound")
    lines = bytes(raw[:position]).split(b"\r\n")
    if lines[0] != b"POST /example HTTP/1.1":
        raise ValueError("example_http_method")
    fields: dict[bytes, bytes] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b": ")
        name = name.lower()
        if not separator or name not in (b"host", b"content-length") or name in fields:
            raise ValueError("example_http_fields")
        fields[name] = value
    length = fields.get(b"content-length", b"")
    if fields.get(b"host") != b"example.com" or not length.isdigit() or len(length) > 7:
        raise ValueError("example_http_length")
    size = int(length)
    if not 0 < size <= 1048576 or str(size).encode() != length:
        raise ValueError("example_http_length")
    body = bytes(raw[position + 4 :])
    if len(body) > size:
        raise ValueError("example_http_extra_bytes")
    return body if len(body) == size else None


def main(_sources: object) -> None:
    decoder, sequence, output = BridgeFrameDecoder(), BridgeSequence(), BridgeWriteBuffer()
    incoming: dict[str, object] = {}
    capsule: bytes | None = None
    started = time.monotonic()
    deadline = started + 8
    for descriptor in (0, 1):
        os.set_blocking(descriptor, False)
    # Startup is bounded and observes the same directional sequence as the host.
    with selectors.DefaultSelector() as startup:
        startup.register(0, selectors.EVENT_READ)
        while capsule is None:
            if time.monotonic() >= deadline:
                return
            for key, _ in startup.select(0.05):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    return
                for frame in decoder.feed(chunk):
                    sequence.observe(BridgeDirection.HOST_TO_CHILD, frame)
                    if frame.kind is BridgeFrameType.SPEC:
                        incoming = json.loads(frame.payload)
                        fixture_timeout = incoming["fixture_timeout"]
                        if (
                            not isinstance(fixture_timeout, (int, float))
                            or type(fixture_timeout) not in (int, float)
                            or not 0 < fixture_timeout <= 8
                        ):
                            raise ValueError("example_fixture_deadline")
                        deadline = started + fixture_timeout
                    elif frame.kind is BridgeFrameType.CAPSULE:
                        capsule = frame.payload
                    elif frame.kind is BridgeFrameType.CANCEL:
                        _write_exit(output, sequence, b"cancelled", deadline)
                        return
    assert capsule is not None
    scenario = incoming["scenario"]
    if scenario == "no_read":
        time.sleep(10)
        return
    if scenario in ("held_pipe", "escaped_pipe"):
        ready_read, ready_write = os.pipe()
        original_sid = os.getsid(0)
        if os.fork() == 0:
            os.close(ready_read)
            if scenario == "escaped_pipe":
                os.setsid()
                if (
                    os.getsid(0) == original_sid
                    or os.getsid(0) != os.getpid()
                    or os.getpgrp() != os.getpid()
                ):
                    os._exit(2)
                _write_raw(
                    encode_bridge_frame(
                        BridgeFrame(BridgeFrameType.NATIVE_STDOUT, b"example-escaped-ready")
                    ),
                    deadline,
                )
            os.write(ready_write, b"r")
            os.close(ready_write)
            time.sleep(10)
            os._exit(0)
        os.close(ready_write)
        with selectors.DefaultSelector() as ready:
            ready.register(ready_read, selectors.EVENT_READ)
            if not ready.select(0.5) or os.read(ready_read, 1) != b"r":
                os._exit(2)
        os.close(ready_read)
        os._exit(0)
    if scenario == "truncated":
        os.write(1, b"HB01\x07\x00\x00\x00\x09partial")
        return
    if scenario == "early_exit":
        _write_exit(output, sequence, b"1", deadline)
        return
    if scenario in ("wrong_digest", "duplicate_request", "response_no_read"):
        request = capsule + b"wrong" if scenario == "wrong_digest" else capsule
        wire = encode_bridge_frame(BridgeFrame(BridgeFrameType.REQUEST, request))
        if scenario == "duplicate_request":
            wire += wire
        _write_raw(wire, deadline)
        time.sleep(10 if scenario == "response_no_read" else 3)
        return
    if scenario == "flood":
        wire = encode_bridge_frame(BridgeFrame(BridgeFrameType.NATIVE_STDOUT, b"x" * 4096))
        while time.monotonic() < deadline:
            _write_raw(wire, deadline)
        return
    inputs = incoming["inputs"]
    assert isinstance(inputs, dict)
    namespace: dict[str, object] = {}
    exec(_ISOLATION, namespace)
    verify = namespace["verify"]
    assert callable(verify)
    verify(inputs, "parent")
    _serve(incoming, capsule, decoder, sequence, output, deadline)


def _write_raw(wire: bytes, deadline: float) -> None:
    offset = 0
    with selectors.DefaultSelector() as selector:
        selector.register(1, selectors.EVENT_WRITE)
        while offset < len(wire):
            if time.monotonic() >= deadline:
                raise ValueError("example_actor_deadline")
            for key, _ in selector.select(0.05):
                try:
                    offset += os.write(key.fd, wire[offset : offset + 511])
                except BlockingIOError:
                    pass


def _write_exit(
    output: BridgeWriteBuffer, sequence: BridgeSequence, payload: bytes, deadline: float
) -> None:
    frame = BridgeFrame(BridgeFrameType.NATIVE_EXIT, payload)
    if not output.enqueue(frame):
        raise ValueError("example_exit_capacity")
    sequence.observe(BridgeDirection.CHILD_TO_HOST, frame)
    while output.observation.pending_bytes:
        offer = output.peek(max_bytes=511)
        with selectors.DefaultSelector() as selector:
            selector.register(1, selectors.EVENT_WRITE)
            if time.monotonic() >= deadline:
                raise ValueError("example_actor_deadline")
            if selector.select(0.05):
                try:
                    output.advance(os.write(1, offer))
                except BlockingIOError:
                    output.advance(0)


def _serve(
    incoming: dict[str, object],
    capsule: bytes,
    decoder: BridgeFrameDecoder,
    sequence: BridgeSequence,
    output: BridgeWriteBuffer,
    deadline: float,
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.setblocking(False)
        metadata = json.dumps({**incoming, "port": listener.getsockname()[1]}).encode()
        child_pending = struct.pack(">I", len(metadata)) + metadata + capsule
        import subprocess

        with owned_fixture_process(
            [sys.executable, "-I", "-c", _CLIENT], {}, stdin=subprocess.PIPE
        ) as child:
            assert child.stdin is not None and child.stdout is not None and child.stderr is not None
            for stream in (child.stdin, child.stdout, child.stderr):
                os.set_blocking(stream.fileno(), False)
            connection: socket.socket | None = None
            child_offset, stderr_count = 0, 0
            request = bytearray()
            response = bytearray()
            http_output: bytes = b""
            http_offset = 0
            requested = False
            child_streams = 2
            pending: deque[BridgeFrame] = deque()
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(0, selectors.EVENT_READ, "host")
                    selector.register(listener, selectors.EVENT_READ, "listener")
                    selector.register(child.stdin, selectors.EVENT_WRITE, "child_stdin")
                    selector.register(child.stdout, selectors.EVENT_READ, "child_stdout")
                    selector.register(child.stderr, selectors.EVENT_READ, "child_stderr")
                    while child_streams:
                        if time.monotonic() >= deadline:
                            raise ValueError("example_actor_deadline")
                        # One legal capsule REQUEST plus bounded client stdout;
                        # a request is not subject to the smaller stdout queue budget.
                        if sum(len(frame.payload) + 9 for frame in pending) > 1048576 + 65536 + 9:
                            raise ValueError("example_pending_bound")
                        while pending:
                            if not output.enqueue(pending[0]):
                                break
                            sequence.observe(BridgeDirection.CHILD_TO_HOST, pending.popleft())
                        try:
                            selector.get_key(1)
                            write_registered = True
                        except KeyError:
                            write_registered = False
                        if output.observation.pending_bytes and not write_registered:
                            selector.register(1, selectors.EVENT_WRITE, "pipe_write")
                        elif not output.observation.pending_bytes and write_registered:
                            selector.unregister(1)
                        for key, mask in selector.select(0.05):
                            if key.data == "pipe_write":
                                try:
                                    output.advance(os.write(1, output.peek(max_bytes=511)))
                                except BlockingIOError:
                                    output.advance(0)
                            elif key.data == "child_stdin":
                                try:
                                    child_offset += os.write(
                                        key.fd, child_pending[child_offset : child_offset + 4096]
                                    )
                                except BlockingIOError:
                                    continue
                                if child_offset == len(child_pending):
                                    selector.unregister(child.stdin)
                                    child.stdin.close()
                            elif key.data == "listener":
                                accepted, _ = listener.accept()
                                accepted.setblocking(False)
                                if connection is not None:
                                    accepted.close()
                                    raise ValueError("example_http_repeat")
                                connection = accepted
                                selector.register(connection, selectors.EVENT_READ, "http")
                            elif key.data == "http":
                                assert connection is not None
                                if mask & selectors.EVENT_WRITE:
                                    try:
                                        http_offset += connection.send(
                                            http_output[http_offset : http_offset + 257]
                                        )
                                    except BlockingIOError:
                                        continue
                                    if http_offset == len(http_output):
                                        selector.unregister(connection)
                                        connection.close()
                                elif mask & selectors.EVENT_READ:
                                    try:
                                        chunk = connection.recv(4096)
                                    except BlockingIOError:
                                        continue
                                    if not chunk or requested:
                                        raise ValueError("example_http_repeat_or_eof")
                                    request.extend(chunk)
                                    if len(request) > 1048576 + 8196:
                                        raise ValueError("example_http_bound")
                                    body = _http_body(request)
                                    if body is not None:
                                        requested = True
                                        pending.append(BridgeFrame(BridgeFrameType.REQUEST, body))
                            else:
                                try:
                                    chunk = os.read(key.fd, 8192)
                                except BlockingIOError:
                                    continue
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                    if key.data == "host":
                                        decoder.finish()
                                        sequence.finish(BridgeDirection.HOST_TO_CHILD)
                                    else:
                                        child_streams -= 1
                                    continue
                                if key.data == "host":
                                    for frame in decoder.feed(chunk):
                                        sequence.observe(BridgeDirection.HOST_TO_CHILD, frame)
                                        if frame.kind is BridgeFrameType.CANCEL:
                                            _write_exit(output, sequence, b"cancelled", deadline)
                                            return
                                        if (
                                            frame.kind is BridgeFrameType.RESPONSE_HEADERS
                                            and frame.payload != b"example-response"
                                        ):
                                            raise ValueError("example_response_headers")
                                        if frame.kind is BridgeFrameType.RESPONSE_CHUNK:
                                            response.extend(frame.payload)
                                            if len(response) > 65536:
                                                raise ValueError("example_response_bound")
                                        if frame.kind is BridgeFrameType.RESPONSE_END:
                                            assert connection is not None
                                            http_output = (
                                                b"HTTP/1.1 200 OK\r\nContent-Length: "
                                                + str(len(response)).encode()
                                                + b"\r\n\r\n"
                                                + response
                                            )
                                            selector.modify(
                                                connection, selectors.EVENT_WRITE, "http"
                                            )
                                elif key.data == "child_stdout":
                                    pending.append(
                                        BridgeFrame(BridgeFrameType.NATIVE_STDOUT, chunk)
                                    )
                                else:
                                    stderr_count += len(chunk)
                                    if stderr_count > 16384:
                                        raise ValueError("example_child_stderr_bound")
                    while (
                        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None
                    ):
                        if time.monotonic() >= deadline:
                            raise ValueError("example_child_deadline")
                        time.sleep(0.01)
                    while pending:
                        frame = pending.popleft()
                        if not output.enqueue(frame):
                            raise ValueError("example_stdout_capacity")
                        sequence.observe(BridgeDirection.CHILD_TO_HOST, frame)
            finally:
                if connection is not None:
                    connection.close()
        _write_exit(output, sequence, b"0" if child.returncode == 0 else b"1", deadline)
