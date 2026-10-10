"""Only execute under the disposable offline native fixture namespace."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

if __name__ == "__main__":
    # Only native execution adds this wrapper-owned readonly import tree.
    sys.path.insert(0, "/opt/example")

from tests.claude_native_request_contract import (
    CAPSULE_BYTES,
    MARKER,
    MODEL,
    NATIVE_SESSION_ID,
    ExpectedNativeRequest,
    NativeRequestContractError,
    environment_text,
    validate_headers,
    validate_request_body,
)

CASES = {"api-key-success", "api-key-reject", "bearer-success", "bearer-reject"}
STAGES = {"initialization", "isolation", "version", "native_stream", "terminal", "cleanup"}
FAILURE_CATEGORIES = {
    "timeout",
    "output_bound",
    "version_invalid",
    "capture_failure",
    "stream_policy",
    "stream_invalid",
    "fixture_failure",
    "init_tools",
    "init_mcp_servers",
    "init_plugins",
    "init_skills",
    "init_permission",
}
stage = "initialization"
policy_shape: dict[str, object] = {}
transport_counts: dict[str, int] = {}
terminal_shape: dict[str, object] = {}


def failure_category(error: BaseException) -> str:
    from hermes_codex_router.claude_stream import ClaudeStreamError
    from tests.native_process_capture import NativeCaptureError

    if isinstance(error, NativeCaptureError):
        if str(error) in FAILURE_CATEGORIES:
            return str(error)
        return {
            "native_fixture_timeout": "timeout",
            "native_fixture_output_bound": "output_bound",
            "native_fixture_version_invalid": "version_invalid",
        }.get(str(error), "capture_failure")
    if isinstance(error, ClaudeStreamError):
        return (
            "stream_policy"
            if str(error) == "claude text-only runtime policy was violated"
            else "stream_invalid"
        )
    return "fixture_failure"


def stream_policy_category(raw: bytes) -> str:
    """Classify rejected init metadata without revealing native values."""
    try:
        event = json.loads(raw)
    except (ValueError, RecursionError):
        return "stream_policy"
    if (
        not isinstance(event, dict)
        or event.get("type") != "system"
        or event.get("subtype") != "init"
    ):
        return "stream_policy"
    for field in ("tools", "mcp_servers", "plugins", "skills"):
        if field in event and (not isinstance(event[field], list) or event[field]):
            return "init_" + field
    if "permissionMode" in event and event["permissionMode"] != "dontAsk":
        return "init_permission"
    return "stream_policy"


def rejected_policy_shape(raw: bytes) -> dict[str, object]:
    """Fixed keys/enums and capped counts only; no native strings or payload."""
    try:
        event = json.loads(raw)
    except (ValueError, RecursionError):
        return {"event": "invalid"}
    if not isinstance(event, dict):
        return {"event": "invalid"}
    if event.get("type") != "system" or event.get("subtype") != "init":
        return {"event": "other"}
    shape: dict[str, object] = {"event": "system_init"}
    for field in ("tools", "mcp_servers", "plugins", "skills"):
        value = event.get(field)
        shape[field] = (
            "absent"
            if field not in event
            else "wrong_type"
            if not isinstance(value, list)
            else "nonempty"
            if value
            else "empty"
        )
        if isinstance(value, list):
            shape[field + "_count"] = min(len(value), 8)
    shape["permission_matches"] = event.get("permissionMode") == "dontAsk"
    plugins = event.get("plugins")
    if isinstance(plugins, list) and plugins:
        plugin = plugins[0]
        shape["first_plugin_object"] = isinstance(plugin, dict)
        if isinstance(plugin, dict):
            for key in ("name", "path", "enabled", "disabled", "scope", "source"):
                shape["first_plugin_has_" + key] = key in plugin
            shape["first_plugin_enabled"] = plugin.get("enabled") is True
            shape["first_plugin_disabled"] = plugin.get("disabled") is True
    return shape


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = False
    block_on_close = True

    def __init__(self, host_port: int, case: str) -> None:
        self.lock = threading.Lock()
        self.requests = self.heads = self.posts = self.connections = self.violations = 0
        self.messages_served = self.timeouts = 0
        self.validated_requests = 0
        self.response_complete = threading.Event()
        self.request_contract: ExpectedNativeRequest | None = None
        self.case = case
        for _attempt in range(4):
            super().__init__(("127.0.0.1", 0), Handler)
            if self.server_port != host_port:
                break
            self.server_close()
        else:
            raise RuntimeError("fixture_port_collision")

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        connection, address = super().get_request()
        with self.lock:
            self.connections += 1
            overflow = self.connections > 8
            if overflow:
                self.violations += 1
        if overflow:
            connection.close()
            raise OSError("fixture connection bound")
        connection.settimeout(2)
        return connection, address

    def handle_error(self, request: object, client_address: object) -> None:
        with self.lock:
            self.violations += 1


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def fixture(self) -> FixtureServer:
        return cast(FixtureServer, self.server)

    def log_message(self, format: str, *args: object) -> None:
        # BaseHTTPRequestHandler catches idle/partial-header socket timeouts.
        # Its fixed timeout diagnostic is counted without retaining its text.
        if format == "Request timed out: %r":
            with self.fixture.lock:
                self.fixture.timeouts += 1
                self.fixture.violations += 1
        pass

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        # Never echo input paths, headers or body data, even inside the fixture.
        with self.fixture.lock:
            self.fixture.violations += 1
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()

    def parse_request(self) -> bool:
        with self.fixture.lock:
            self.fixture.requests += 1
            overflow = self.fixture.requests > 4
        parsed = super().parse_request()
        if parsed and overflow:
            self.send_error(429)
            return False
        return parsed

    def _respond(self, code: int, content_type: str, data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if data:
            self.wfile.write(data)

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
            unique = self.fixture.posts == 1
        if self.path not in {"/v1/messages", "/v1/messages?beta=true"} or not unique:
            self.send_error(400)
            return
        try:
            size = self._post_headers()
        except NativeRequestContractError:
            self.send_error(400)
            return
        raw = self.rfile.read(size)
        expected = self.fixture.request_contract
        if len(raw) != size or expected is None:
            self.send_error(400)
            return
        try:
            reply_model, reply_marker = self._post_body(raw, expected)
        except NativeRequestContractError:
            self.send_error(400)
            return
        with self.fixture.lock:
            self.fixture.validated_requests += 1
        if self.fixture.case.endswith("reject"):
            error = {
                "type": "error",
                "error": {"type": "overloaded_error", "message": "fictional rejection"},
            }
            self._respond(529, "application/json", json.dumps(error).encode())
            with self.fixture.lock:
                self.fixture.messages_served += 1
            self.fixture.response_complete.set()
            return
        message = {
            "id": "msg_example_native",
            "type": "message",
            "role": "assistant",
            "model": reply_model,
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
                "delta": {"type": "text_delta", "text": reply_marker},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
        encoded = "".join(
            "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events
        )
        self._respond(200, "text/event-stream", encoded.encode())
        with self.fixture.lock:
            self.fixture.messages_served += 1
        self.fixture.response_complete.set()

    def _post_headers(self) -> int:
        return validate_headers(
            list(self.headers.raw_items()),
            port=self.fixture.server_port,
            case=self.fixture.case,
            method="POST",
        )

    def _post_body(self, raw: bytes, expected: ExpectedNativeRequest) -> tuple[str, str]:
        validate_request_body(raw, expected)
        return MODEL, MARKER


def update_terminal_shape(raw: bytes, shape: dict[str, object]) -> None:
    """Retain only bounded diagnostic enums, booleans and exact status codes."""
    text = raw.decode("utf-8").strip()
    event = json.loads(text) if text else {}
    if not isinstance(event, dict):
        return
    if event.get("type") == "assistant":
        error = event.get("error")
        shape["assistant_error_present"] = error is not None
        shape["latest_assistant_error"] = (
            None
            if error is None
            else error
            if isinstance(error, str)
            and error
            in {
                "overloaded",
                "unknown",
                "rate_limit",
                "authentication_failed",
                "billing_error",
                "model_not_found",
            }
            else "other"
        )
    if event.get("type") == "result":
        subtype = event.get("subtype")
        shape["subtype"] = (
            subtype
            if isinstance(subtype, str)
            and subtype
            in {
                "success",
                "error_during_execution",
                "error_max_turns",
                "error_max_budget_usd",
                "error_max_structured_output_retries",
            }
            else "other"
        )
        shape["is_error"] = event.get("is_error") is True
        shape["error_is_boolean"] = type(event.get("is_error")) is bool
        status = event.get("api_error_status")
        shape["api_error_status"] = (
            None
            if status is None
            else status
            if type(status) is int and status in {401, 429, 529}
            else "other"
        )
        shape["errors_is_list"] = isinstance(event.get("errors"), list)
        shape["result_is_text"] = isinstance(event.get("result"), str)


def main() -> None:
    from hermes_codex_router.claude_stream import (
        MAX_CLAUDE_OUTPUT_BYTES,
        MAX_CLAUDE_STDERR_BYTES,
        ClaudeStreamError,
        ClaudeStreamReader,
        ClaudeTerminalFailure,
        ClaudeVisibleAssistant,
        parse_claude_stream,
    )
    from tests.claude_native_request_contract import DUMMY
    from tests.native_process_capture import NativeCaptureError, capture_owned_process

    global stage, policy_shape, transport_counts
    if sys.version_info < (3, 11):
        raise NativeCaptureError("native_fixture_runtime_unsupported")
    stage = "isolation"
    host_port, sentinel, case = int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    argv = json.loads(sys.argv[4])
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        raise NativeCaptureError("fixture_argv_invalid")
    if case not in CASES:
        raise NativeCaptureError("fixture_case_invalid")
    host_hidden = not sentinel.exists()
    host_blocked = False
    with socket.socket() as probe:
        probe.settimeout(1)
        try:
            probe.connect(("127.0.0.1", host_port))
        except OSError:
            host_blocked = True
    server = FixtureServer(host_port, case)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05))
    thread.start()
    try:
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
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:" + str(server.server_port),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES": "0",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1024",
        }
        environment[
            "ANTHROPIC_AUTH_TOKEN" if case.startswith("bearer") else "ANTHROPIC_API_KEY"
        ] = DUMMY
        stage = "version"
        code, version = capture_owned_process(
            ["/opt/example/claude", "--version"],
            environment,
            timeout=10,
            stdout_limit=1024,
            stderr_limit=4096,
        )
        version_text = version.decode("ascii").strip()
        if (
            code != 0
            or len(version_text) > 64
            or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+ \(Claude Code\)", version_text)
        ):
            raise NativeCaptureError("native_fixture_version_invalid")
        # Host-chosen scaffold inputs are captured before the native Messages
        # request. Never infer expected material/environment from child output.
        server.request_contract = ExpectedNativeRequest(
            version_text.removesuffix(" (Claude Code)"),
            environment_text(
                "Linux " + os.uname().release, datetime.now(timezone.utc).date().isoformat()
            ),
        )
        stage = "native_stream"

        class DiagnosticReader(ClaudeStreamReader):
            def _line(self, raw: bytes) -> None:
                global terminal_shape
                try:
                    super()._line(raw)
                except ClaudeStreamError as error:
                    if str(error) == "claude text-only runtime policy was violated":
                        global policy_shape
                        policy_shape = rejected_policy_shape(raw)
                        raise NativeCaptureError(stream_policy_category(raw)) from error
                    raise
                update_terminal_shape(raw, terminal_shape)

        visible_messages = 0

        def count_visible(_message: ClaudeVisibleAssistant) -> None:
            nonlocal visible_messages
            visible_messages += 1

        terminal_shape["assistant_error_present"] = False
        reader = DiagnosticReader(
            expected_session_id=NATIVE_SESSION_ID, on_visible_assistant=count_visible
        )
        code, _ = capture_owned_process(
            argv,
            environment,
            timeout=75,
            stdout_limit=MAX_CLAUDE_OUTPUT_BYTES,
            stderr_limit=MAX_CLAUDE_STDERR_BYTES,
            on_stdout=reader.feed,
        )
        stage = "terminal"
        success, failure, failure_code = False, False, None
        try:
            result = parse_claude_stream(
                reader.finish(),
                expected_session_id=NATIVE_SESSION_ID,
                requested_model=MODEL,
                returncode=code,
            )
            success = result.text == MARKER and result.model == MODEL
        except ClaudeTerminalFailure as error:
            failure = True
            failure_code = error.code
        report = {
            "case": case,
            "native_version": version_text,
            "terminal_shape": dict(terminal_shape),
            "visible_messages": visible_messages,
            "parser_python_version": list(sys.version_info[:2]),
            "host_files_hidden": host_hidden,
            "host_loopback_blocked": host_blocked,
            "ports_distinct": server.server_port != host_port,
            "exit_code": code,
            "terminal_success": success,
            "terminal_failure": failure,
            "failure_code": failure_code,
            "request_contract": {
                "validated_requests": server.validated_requests,
                "selected_capsule_sha256": hashlib.sha256(CAPSULE_BYTES).hexdigest(),
            },
        }
    finally:
        failed_stage = stage
        stage = "cleanup"
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        if thread.is_alive():
            raise NativeCaptureError("fixture_listener_cleanup_failed")
        with server.lock:
            transport_counts = {
                key: min(getattr(server, key), 16)
                for key in (
                    "connections",
                    "timeouts",
                    "requests",
                    "heads",
                    "posts",
                    "messages_served",
                    "violations",
                )
            }
        stage = failed_stage
    # Only sanitized evidence, after all native execution and listener cleanup.
    with server.lock:
        report.update(
            connections=server.connections,
            timeouts=server.timeouts,
            requests=server.requests,
            heads=server.heads,
            posts=server.posts,
            messages_served=server.messages_served,
            violations=server.violations,
        )
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    try:
        main()
    except BaseException as error:
        print(
            json.dumps(
                {
                    "fixture_failed": True,
                    "stage": stage,
                    "category": failure_category(error),
                    "policy_shape": policy_shape,
                    "transport_counts": transport_counts,
                    "terminal_shape": terminal_shape,
                }
            ),
            flush=True,
        )
        raise SystemExit(1)
