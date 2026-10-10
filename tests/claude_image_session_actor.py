"""Two sequential actual CLI processes, exclusively inside a disposable namespace."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Iterator, cast

if __name__ == "__main__":
    sys.path.insert(0, "/opt/example")

from tests.claude_image_request_contract import (
    MARKERS,
    MISSING_SESSION_ID,
    input_message,
    validate_image_request,
    validate_missing_session_result,
)
from tests.claude_native_request_contract import (
    DUMMY,
    MODEL,
    NATIVE_SESSION_ID,
    ExpectedNativeRequest,
    NativeRequestContractError,
    environment_text,
    validate_headers,
)
from tests.claude_native_transport_actor import FixtureServer, Handler
from tests.native_process_capture import NativeCaptureError, capture_owned_process

STAGES = {"isolation", "version", "fresh", "resume", "missing_session", "cleanup", "complete"}
stage = "isolation"
COUNTERS = (
    "connections",
    "timeouts",
    "requests",
    "heads",
    "posts",
    "messages_served",
    "violations",
    "validated_requests",
)


class ImageServer(FixtureServer):
    def __init__(self, host_port: int, *, phase: int = 0) -> None:
        if type(phase) is not int or phase not in (0, 1, 2):
            raise NativeCaptureError("native_fixture_phase_invalid")
        self.phase = phase
        super().__init__(host_port, "api-key-success")
        self.RequestHandlerClass = ImageHandler


class ImageHandler(Handler):
    @property
    def fixture(self) -> ImageServer:
        return cast(ImageServer, self.server)

    def parse_request(self) -> bool:
        # Each independently drained endpoint owns at most HEAD + POST.
        # Do not change the older text fixture's four-request contract.
        with self.fixture.lock:
            self.fixture.requests += 1
            overflow = self.fixture.requests > 2
        parsed = BaseHTTPRequestHandler.parse_request(self)
        if parsed and overflow:
            self.send_error(429)
            return False
        return parsed

    def do_HEAD(self) -> None:
        with self.fixture.lock:
            self.fixture.heads += 1
            valid = self.fixture.heads <= 1
        if self.path != "/api/hello" or not valid:
            self.send_error(400)
            return
        try:
            validate_headers(
                list(self.headers.raw_items()),
                port=self.fixture.server_port,
                case=self.fixture.case,
                method="HEAD",
            )
        except NativeRequestContractError:
            self.send_error(400)
            return
        self._respond(200, "application/json", b"")

    def do_POST(self) -> None:
        with self.fixture.lock:
            self.fixture.posts += 1
            phase = self.fixture.phase
            valid = self.fixture.posts == 1 and phase in (0, 1)
        if self.path not in {"/v1/messages", "/v1/messages?beta=true"} or not valid:
            self.send_error(400)
            return
        try:
            size = validate_headers(
                list(self.headers.raw_items()),
                port=self.fixture.server_port,
                case=self.fixture.case,
                method="POST",
            )
            raw = self.rfile.read(size)
            expected = self.fixture.request_contract
            if len(raw) != size or expected is None:
                raise NativeRequestContractError("native_image_request_invalid")
            validate_image_request(raw, expected, phase, uid=os.getuid())
        except NativeRequestContractError:
            self.send_error(400)
            return
        with self.fixture.lock:
            self.fixture.validated_requests += 1
        message = {
            "id": "msg_example_image_" + str(phase),
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        events = [
            {"type": "message_start", "message": message},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": MARKERS[phase]},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
        self._respond(
            200,
            "text/event-stream",
            "".join(
                "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                for event in events
            ).encode(),
        )
        with self.fixture.lock:
            self.fixture.messages_served += 1
        self.fixture.response_complete.set()


@contextmanager
def image_endpoint(host_port: int, phase: int) -> Iterator[ImageServer]:
    """Drain and join every handler before the next native process can start."""
    server = ImageServer(host_port, phase=phase)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05))
    thread.start()
    try:
        yield server
    finally:
        global stage
        previous_stage = stage
        stage = "cleanup"
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        if thread.is_alive():
            raise NativeCaptureError("native_fixture_listener_cleanup_failed")
        stage = previous_stage


def main() -> None:
    from hermes_codex_router.claude_stream import ClaudeStreamReader, parse_claude_stream

    global stage
    host_port, sentinel = int(sys.argv[1]), Path(sys.argv[2])
    argv = json.loads(sys.argv[3])
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise NativeCaptureError("native_fixture_argv_invalid")
    hidden = not sentinel.exists()
    blocked = False
    with socket.socket() as probe:
        probe.settimeout(1)
        try:
            probe.connect(("127.0.0.1", host_port))
        except OSError:
            blocked = True
    environment = {
        "HOME": "/home/example",
        "CLAUDE_CONFIG_DIR": "/home/example/.claude",
        "XDG_CONFIG_HOME": "/home/example/.config",
        "XDG_CACHE_HOME": "/home/example/.cache",
        "XDG_DATA_HOME": "/home/example/.local/share",
        "XDG_STATE_HOME": "/home/example/.local/state",
        "TMPDIR": "/tmp",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "ANTHROPIC_API_KEY": DUMMY,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
        "CLAUDE_CODE_MAX_RETRIES": "0",
        "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES": "0",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1024",
    }
    stage = "version"
    code, version = capture_owned_process(
        ["/opt/example/claude", "--version"],
        environment,
        timeout=10,
        stdout_limit=1024,
        stderr_limit=4096,
    )
    if code != 0 or version.strip() != b"2.1.285 (Claude Code)":
        raise NativeCaptureError("native_fixture_version_invalid")
    expected = ExpectedNativeRequest(
        "2.1.285",
        environment_text(
            "Linux " + os.uname().release, datetime.now(timezone.utc).date().isoformat()
        ),
    )
    completed = []
    endpoints = []
    for phase in (0, 1):
        stage = "fresh" if phase == 0 else "resume"
        native_argv = list(argv)
        if phase:
            index = native_argv.index("--session-id")
            native_argv[index : index + 2] = ["--resume", NATIVE_SESSION_ID]
        with image_endpoint(host_port, phase) as server:
            server.request_contract = expected
            environment["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:" + str(server.server_port)
            visible = []
            reader = ClaudeStreamReader(
                expected_session_id=NATIVE_SESSION_ID,
                on_visible_assistant=visible.append,
            )
            code, _ = capture_owned_process(
                native_argv,
                environment,
                timeout=40,
                stdout_limit=4 * 1024 * 1024,
                stderr_limit=65536,
                on_stdout=reader.feed,
                stdin_data=input_message(phase),
                stdin_limit=16384,
            )
            output = reader.finish()
            events = [json.loads(line) for line in output.splitlines() if line.strip()]
            init = [
                event
                for event in events
                if event.get("type") == "system" and event.get("subtype") == "init"
            ]
            if (
                len(init) != 1
                or any(
                    init[0].get(key) != [] for key in ("tools", "mcp_servers", "plugins", "skills")
                )
                or init[0].get("permissionMode") != "dontAsk"
            ):
                raise NativeCaptureError("native_fixture_init_policy_unproven")
            result = parse_claude_stream(
                output,
                expected_session_id=NATIVE_SESSION_ID,
                requested_model=MODEL,
                returncode=code,
            )
            if (
                result.text != MARKERS[phase]
                or result.model != MODEL
                or len(visible) != 1
                or visible[0].text != MARKERS[phase]
            ):
                raise NativeCaptureError("native_fixture_completion_unproven")
        # Child/group and all HTTP handlers have been cleaned before advancing.
        endpoints.append(server)
        if server.posts != 1 or server.validated_requests != 1 or server.messages_served != 1:
            raise NativeCaptureError("native_fixture_completion_unproven")
        sessions = list(Path("/home/example/.claude/projects").rglob(NATIVE_SESSION_ID + ".jsonl"))
        if len(sessions) != 1 or not 0 < sessions[0].stat().st_size <= 1024 * 1024:
            raise NativeCaptureError("native_fixture_session_store_unproven")
        completed.append(True)

    stage = "missing_session"
    missing_id = MISSING_SESSION_ID
    missing = list(argv)
    index = missing.index("--session-id")
    missing[index : index + 2] = ["--resume", missing_id]
    store = Path("/home/example/.claude/projects")
    before_files = sorted(store.rglob("*.jsonl"))
    if before_files != sorted(sessions) or any(
        path.name == missing_id + ".jsonl" for path in before_files
    ):
        raise NativeCaptureError("native_fixture_missing_source_unproven")
    before_digest = hashlib.sha256(sessions[0].read_bytes()).hexdigest()
    missing_input = json.loads(input_message(1))
    missing_input["session_id"] = missing_id
    with image_endpoint(host_port, 2) as missing_server:
        environment["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:" + str(missing_server.server_port)
        code, missing_output = capture_owned_process(
            missing,
            environment,
            timeout=15,
            stdout_limit=65536,
            stderr_limit=65536,
            stdin_data=(json.dumps(missing_input) + "\n").encode(),
            stdin_limit=16384,
        )
    validate_missing_session_result(missing_output, returncode=code)
    endpoints.append(missing_server)
    after_files = sorted(store.rglob("*.jsonl"))
    unchanged = (
        after_files == before_files
        and 0 < sessions[0].stat().st_size <= 1024 * 1024
        and hashlib.sha256(sessions[0].read_bytes()).hexdigest() == before_digest
    )
    report = {
        "native_version": version.decode("ascii").strip(),
        "host_files_hidden": hidden,
        "host_loopback_blocked": blocked,
        "ports_distinct": all(endpoint.server_port != host_port for endpoint in endpoints),
        "completed": completed,
        "missing_exit_code": code,
        "missing_no_messages": missing_server.posts == 0,
        "missing_refusal_validated": True,
        "missing_store_unchanged": unchanged,
        "missing_requests": missing_server.requests,
        "missing_heads": missing_server.heads,
        **{key: sum(getattr(endpoint, key) for endpoint in endpoints) for key in COUNTERS},
    }
    stage = "complete"
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        print(json.dumps({"fixture_failed": True, "stage": stage}), flush=True)
        raise SystemExit(1)
