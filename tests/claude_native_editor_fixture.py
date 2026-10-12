"""One bounded native file-tool script with a real host and fictional receipts.

This fixture never installs a runtime or relaxes production ownership checks.
The host/state/receipt keys stay outside the offline CLI namespace. Copied,
readonly test code is not an attestation of installed immutable runtime custody.
"""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any, cast

from hermes_codex_router.claude_file_policy import file_tool_argv, require_file_tool_event
from hermes_codex_router.claude_file_sandbox import FileToolSandboxConfig
from hermes_codex_router.claude_permission_protocol import ProtectedPayload
from hermes_codex_router.claude_stream import ClaudeStreamReader, parse_claude_stream

MARKER = "example-editor-complete"
TOOLS = {"Read", "Glob", "Grep", "Write", "Edit"}
PHASE = "initialization"


def script(root: Path) -> list[tuple[str, dict[str, Any]] | None]:
    return [
        ("Write", {"file_path": str(root / "allowed.txt"), "content": "example-allowed\n"}),
        None,
        ("Write", {"file_path": str(root / "allowed.txt"), "content": "example-denied\n"}),
        ("Read", {"file_path": str(root / "seed.txt")}),
        (
            "Edit",
            {
                "file_path": str(root / "seed.txt"),
                "old_string": "example-before",
                "new_string": "example-after",
                "replace_all": False,
            },
        ),
        (
            "Edit",
            {
                "file_path": str(root / "seed.txt"),
                "old_string": "example-after",
                "new_string": "example-denied",
                "replace_all": False,
            },
        ),
        None,
    ]


class _OneDecision(threading.Event):
    def wait(self, timeout: float | None = None) -> bool:
        released = super().wait(timeout)
        if released:
            self.clear()
        return released


def run_native_editor(case: Any, executable: Path, digest: str, version: str) -> dict[str, Any]:
    from tests.claude_native_transport_fixture import (
        _copy_native_binary,
        build_native_fixture_argv,
        validate_native_identity,
    )
    from tests.native_process_capture import capture_owned_process
    from tests.native_runtime_mounts import native_runtime_mounts
    from tests.test_claude_permission_host_roundtrip import PermissionHostRoundtripTests

    validate_native_identity(digest, version, digest, version)
    fixture = PermissionHostRoundtripTests()
    try:
        fixture.setUp()
    except BaseException:
        fixture.doCleanups()
        raise
    fixture.peer.release = _OneDecision()
    root = fixture.root
    (root / "seed.txt").write_bytes(b"example-before\n")
    expected = [step for step in script(root) if step is not None and step[0] != "Read"]
    responses = ("allow", "deny", "allow", "deny")
    outcome: dict[str, Any] = {}
    checked: list[bool] = []
    nonces: list[str] = []
    thread: threading.Thread | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="example-native-editor-") as directory:
            binary = Path(directory) / "claude"
            case.assertEqual(_copy_native_binary(executable, binary), digest)
            # Only the argv builder's interpreter field is used. This is not a
            # production sandbox object or a waiver of its ownership validation.
            from types import SimpleNamespace

            settings = cast(
                FileToolSandboxConfig, SimpleNamespace(python_executable=Path("/usr/bin/python3"))
            )
            native_argv = list(file_tool_argv(build_native_fixture_argv(root), settings))
            native_argv.remove("--no-session-persistence")
            native_argv[native_argv.index("--session-id") + 1] = fixture.native_id
            native_argv[native_argv.index("--max-turns") + 1] = "5"
            source = Path(__file__).resolve().parents[1] / "src"
            argv = [
                "/usr/bin/bwrap",
                "--die-with-parent",
                "--new-session",
                "--unshare-net",
                "--unshare-pid",
                "--unshare-ipc",
                *native_runtime_mounts(),
                "--tmpfs",
                "/tmp",
                "--dir",
                "/home/example/.claude",
                "--bind",
                str(root),
                str(root),
                "--ro-bind",
                str(root / ".git"),
                str(root / ".git"),
                "--ro-bind",
                str(binary),
                "/opt/example/claude",
                "--ro-bind",
                str(source),
                "/usr/lib/python3/dist-packages",
                "--ro-bind",
                str(Path(__file__).with_name("native_process_capture.py")),
                "/opt/example/native_process_capture.py",
                "--ro-bind",
                str(Path(__file__)),
                "/opt/example/actor.py",
                "--ro-bind",
                str(fixture.server.path),
                "/run/hub-permission.sock",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--chdir",
                str(root),
                "--",
                "/usr/bin/python3",
                "-I",
                "/opt/example/actor.py",
                json.dumps(native_argv),
                str(fixture.config.state_path),
                version,
            ]
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(1)
                argv.append(str(listener.getsockname()[1]))

                def capture() -> None:
                    try:
                        code, raw = capture_owned_process(
                            argv,
                            {"PATH": "/usr/bin:/bin", "HOME": "/home/example", "LANG": "C.UTF-8"},
                            timeout=95,
                            stdout_limit=8192,
                            stderr_limit=65536,
                        )
                        outcome.update(code=code, report=json.loads(raw))
                    except BaseException as error:
                        outcome["error"] = error

                thread = threading.Thread(target=capture)
                thread.start()
                while thread.is_alive():
                    if len(fixture.peer.requests) > len(nonces):
                        index = len(nonces)
                        payload = ProtectedPayload.parse(str(fixture.peer.requests[-1]["payload"]))
                        case.assertLess(index, len(expected))
                        case.assertEqual((payload.tool_name, payload.tool_input), expected[index])
                        case.assertEqual(payload.session_id, fixture.native_id)
                        case.assertEqual(payload.launch_epoch, fixture.launch.epoch)
                        case.assertEqual(fixture._statuses()[-1], "pending")
                        seed = b"example-before\n" if index < 3 else b"example-after\n"
                        checked.append(
                            (root / "seed.txt").read_bytes() == seed
                            and not (root / "denied.txt").exists()
                            and (
                                not (root / "allowed.txt").exists()
                                if index == 0
                                else (root / "allowed.txt").read_bytes() == b"example-allowed\n"
                            )
                        )
                        case.assertTrue(checked[-1])
                        nonces.append(payload.request_nonce)
                        fixture.peer.decision = responses[index]
                        fixture.peer.release.set()
                    time.sleep(0.01)
                thread.join(1)
            if "error" in outcome:
                raise outcome["error"]
            case.assertEqual(outcome["code"], 0, str(outcome["report"]))
            report = outcome["report"]
            case.assertEqual(report["native_version"], version)
            report.update(
                permission_decisions=fixture._statuses(),
                distinct_requests=len(set(nonces)),
                unchanged_before_each_decision=len(checked) == 4 and all(checked),
                exact_final_bytes=(root / "allowed.txt").read_bytes() == b"example-allowed\n"
                and (root / "seed.txt").read_bytes() == b"example-after\n"
                and not (root / "denied.txt").exists(),
            )
            return report
    finally:
        fixture.peer.decision = "deny"
        try:
            fixture.server.close()
        finally:
            fixture.peer.release.set()
            try:
                if thread is not None:
                    thread.join(100)
                    case.assertFalse(thread.is_alive(), "owned offline actor did not stop")
            finally:
                try:
                    fixture.tearDown()
                finally:
                    fixture.doCleanups()


def actor() -> dict[str, Any]:
    global PHASE
    sys.path.insert(0, "/opt/example")
    from native_process_capture import capture_owned_process

    argv, sentinel, expected_version, host_port = sys.argv[1:]
    commands = json.loads(argv)
    steps = script(Path.cwd())
    errors: list[bool] = []
    counts = {"posts": 0, "heads": 0, "violations": 0}
    lock = threading.Lock()
    restored_history = False

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def server_bind(self) -> None:
            TCPServer.server_bind(self)
            self.server_name, self.server_port = "example-local", self.server_address[1]

        def get_request(self) -> tuple[socket.socket, Any]:
            conn, address = super().get_request()
            conn.settimeout(3)
            return conn, address

        def handle_error(self, request: object, client_address: object) -> None:
            with lock:
                counts["violations"] += 1

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def parse_request(self) -> bool:
            valid = super().parse_request()
            if not valid or self.command not in {"HEAD", "POST"}:
                with lock:
                    counts["violations"] += 1
                if valid:
                    self.respond(400, "application/json", b"{}")
                return False
            return True

        def respond(self, code: int, content_type: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

        def do_HEAD(self) -> None:
            with lock:
                counts["heads"] += 1
                valid = counts["heads"] <= 2
            if self.path != "/api/hello" or not valid:
                with lock:
                    counts["violations"] += 1
                self.respond(400, "application/json", b"")
                return
            self.respond(200, "application/json", b"")

        def do_POST(self) -> None:
            nonlocal restored_history
            with lock:
                counts["posts"] += 1
                ordinal = counts["posts"]
            size = int(self.headers.get("Content-Length", "-1"))
            if (
                not 0 < size <= 262144
                or len(steps) == 0
                or self.path not in {"/v1/messages", "/v1/messages?beta=true"}
            ):
                with lock:
                    counts["violations"] += 1
                self.respond(400, "application/json", b"{}")
                return
            body = json.loads(self.rfile.read(size))
            if (
                body.get("model") != "claude-opus-5-5"
                or body.get("stream") is not True
                or {tool["name"] for tool in body.get("tools", [])} != TOOLS
                or len(body["tools"]) != len(TOOLS)
            ):
                with lock:
                    counts["violations"] += 1
                self.respond(400, "application/json", b"{}")
                return
            if ordinal == 3:
                blocks = [
                    (message["role"], block)
                    for message in body["messages"]
                    if isinstance(message.get("content"), list)
                    for block in message["content"]
                ]
                first = script(Path.cwd())[0]
                assert first is not None
                restored_history = (
                    any(
                        role == "assistant"
                        and block.get("type") == "tool_use"
                        and block.get("id") == "toolu_example_6"
                        and block.get("name") == first[0]
                        and block.get("input") == first[1]
                        for role, block in blocks
                    )
                    and any(
                        role == "user"
                        and block.get("type") == "tool_result"
                        and block.get("tool_use_id") == "toolu_example_6"
                        and not block.get("is_error", False)
                        for role, block in blocks
                    )
                    and any(
                        role == "assistant"
                        and block.get("type") == "text"
                        and block.get("text") == MARKER
                        for role, block in blocks
                    )
                )
                if not restored_history:
                    with lock:
                        counts["violations"] += 1
                    self.respond(400, "application/json", b"{}")
                    return
            with lock:
                selected = steps.pop(0)
            block = (
                {
                    "type": "tool_use",
                    "id": "toolu_example_" + str(len(steps)),
                    "name": selected[0],
                    "input": {},
                }
                if selected
                else {"type": "text", "text": ""}
            )
            events = [
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_example_" + str(len(steps)),
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-opus-5-5",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1},
                    },
                },
                {"type": "content_block_start", "index": 0, "content_block": block},
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": json.dumps(selected[1])}
                    if selected
                    else {"type": "text_delta", "text": MARKER},
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": "tool_use" if selected else "end_turn",
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": 1},
                },
                {"type": "message_stop"},
            ]
            self.respond(
                200,
                "text/event-stream",
                "".join(
                    "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                    for event in events
                ).encode(),
            )

    host_blocked = False
    with socket.socket() as probe:
        probe.settimeout(1)
        try:
            probe.connect(("127.0.0.1", int(host_port)))
        except OSError:
            host_blocked = True
    server = Server(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05))
    thread.start()
    env = {
        "HOME": "/home/example",
        "CLAUDE_CONFIG_DIR": "/home/example/.claude",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "ANTHROPIC_AUTH_TOKEN": "example-fixture-no-real-credential",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:" + str(server.server_port),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
        "CLAUDE_CODE_MAX_RETRIES": "0",
        "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES": "0",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1024",
        "HUB_CLAUDE_PERMISSION_SOCKET": "/run/hub-permission.sock",
    }
    try:
        PHASE = "version"
        code, raw = capture_owned_process(
            [commands[0], "--version"], env, timeout=10, stdout_limit=1024, stderr_limit=4096
        )
        if code != 0 or raw.decode("ascii").strip() != expected_version:
            raise RuntimeError("native_identity_mismatch")

        def observe(event: dict[str, object]) -> None:
            require_file_tool_event(event)
            message = event.get("message")
            if isinstance(message, dict):
                for block in message.get("content", []):
                    if block.get("type") == "tool_result":
                        errors.append(block.get("is_error", False) is True)

        identity = commands[commands.index("--session-id") + 1]
        for resumed in (False, True):
            PHASE = "resume" if resumed else "first_turn"
            selected_argv = list(commands)
            if resumed:
                selected_argv[selected_argv.index("--session-id")] = "--resume"
            reader = ClaudeStreamReader(expected_session_id=identity, event_policy=observe)
            code, _ = capture_owned_process(
                selected_argv,
                env,
                timeout=35,
                stdout_limit=2097152,
                stderr_limit=65536,
                on_stdout=reader.feed,
            )
            result = parse_claude_stream(
                reader.finish(),
                expected_session_id=identity,
                requested_model="claude-opus-5-5",
                returncode=code,
            )
            if result.text != MARKER:
                raise RuntimeError("native_terminal_unproven")
        return {
            "native_version": expected_version,
            "native_successes": 2,
            "native_tool_errors": errors,
            **counts,
            "restored_history": restored_history,
            "host_files_hidden": not Path(sentinel).exists(),
            "host_loopback_blocked": host_blocked,
        }
    finally:
        try:
            server.shutdown()
        finally:
            try:
                server.server_close()
            finally:
                thread.join(3)
                if thread.is_alive():
                    raise RuntimeError("fixture_cleanup_failed")


if __name__ == "__main__":
    try:
        report = actor()
    except Exception as error:
        print(
            json.dumps({"fixture_failed": True, "phase": PHASE, "category": type(error).__name__})
        )
        raise SystemExit(1) from None
    print(json.dumps(report, separators=(",", ":")))
