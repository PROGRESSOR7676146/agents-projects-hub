"""Offline characterization only; no Hub admission, steering or permission authority."""

from __future__ import annotations

import json
import os
import selectors
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

INITIAL = "example-initial-task"
CORRECTION = "example-mid-turn-correction"
SESSION = "00000000-0000-4000-8000-000000000001"
INPUT_IDS = ("00000000-0000-4000-8000-000000000001", "019abcde-1234-7fff-8fff-0123456789ab")
SCENARIOS = ("tool-loop", "text-final")


def validate_observation(report: dict[str, Any], scenario: str) -> None:
    """A successful process or an echo alone cannot prove same-turn consumption."""
    expected = {
        "scenario": scenario,
        "exit_code": 0,
        "posts": 2,
        "violations": 0,
        "correction_in_requests": [False, True],
        "initial_in_requests": [True, True],
        "tool_result_in_requests": [False, scenario == "tool-loop"],
        "successful_read_in_requests": [False, scenario == "tool-loop"],
        "stdin_open_until_results": True,
        "results": 1 if scenario == "tool-loop" else 2,
        "successes": [True] if scenario == "tool-loop" else [True, True],
        "echoed_ids": list(INPUT_IDS),
        "exact_echoes": True,
        "correction_before_first_result": scenario == "tool-loop",
        "same_session": True,
        "stderr_bytes": 0,
        "native_version": "2.1.296 (Claude Code)",
        "host_files_hidden": True,
        "host_loopback_blocked": True,
    }
    if scenario not in SCENARIOS or any(
        type(report.get(key)) is not type(value) or report.get(key) != value
        for key, value in expected.items()
    ):
        raise ValueError("example_native_input_observation_unproven")


def run_native_input(executable: Path, digest: str, scenario: str) -> dict[str, Any]:
    from tests.claude_native_transport_fixture import _copy_native_binary
    from tests.native_process_capture import capture_owned_process
    from tests.native_runtime_mounts import native_runtime_mounts

    if scenario not in SCENARIOS:
        raise ValueError("example_native_input_scenario_invalid")
    with tempfile.TemporaryDirectory(prefix="example-native-input-") as directory:
        temporary = Path(directory)
        project = temporary / "project"
        project.mkdir()
        (project / "seed.txt").write_text("example-fictional-seed\n")
        sentinel = temporary / "host-sentinel"
        sentinel.touch()
        binary = temporary / "claude"
        if _copy_native_binary(executable, binary) != digest:
            raise ValueError("example_native_input_binary_changed")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
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
                str(project),
                "/project",
                "--ro-bind",
                str(binary),
                "/opt/example/claude",
                "--ro-bind",
                str(Path(__file__)),
                "/opt/example/actor.py",
                "--ro-bind",
                str(Path(__file__).with_name("native_process_capture.py")),
                "/opt/example/native_process_capture.py",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--chdir",
                "/project",
                "--",
                "/usr/bin/python3",
                "-I",
                "/opt/example/actor.py",
                scenario,
                str(sentinel),
                str(listener.getsockname()[1]),
            ]
            code, raw = capture_owned_process(
                argv,
                {"PATH": "/usr/bin:/bin", "HOME": "/home/example", "LANG": "C.UTF-8"},
                timeout=70,
                stdout_limit=8192,
                stderr_limit=65536,
            )
        if code != 0:
            raise ValueError("example_native_input_actor_failed")
        report = json.loads(raw)
        validate_observation(report, scenario)
        return report


def _response(tool: bool) -> bytes:
    block = (
        {"type": "tool_use", "id": "toolu_example", "name": "Read", "input": {}}
        if tool
        else {"type": "text", "text": ""}
    )
    delta = (
        {"type": "input_json_delta", "partial_json": '{"file_path":"/project/seed.txt"}'}
        if tool
        else {"type": "text_delta", "text": "example-complete"}
    )
    events = [
        {
            "type": "message_start",
            "message": {
                "id": "msg_example",
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
        {"type": "content_block_delta", "index": 0, "delta": delta},
        {"type": "content_block_stop", "index": 0},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use" if tool else "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 1},
        },
        {"type": "message_stop"},
    ]
    return "".join(
        "event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events
    ).encode()


def _capture(
    argv: list[str],
    environment: dict[str, str],
    first: threading.Event,
    release: threading.Event,
    scenario: str,
) -> tuple[int, list[dict[str, Any]], int, bool]:
    from native_process_capture import owned_fixture_process

    inputs = []
    for text, identity in zip((INITIAL, CORRECTION), INPUT_IDS, strict=True):
        inputs.append(
            (
                json.dumps(
                    {
                        "type": "user",
                        "session_id": SESSION,
                        "uuid": identity,
                        "parent_tool_use_id": None,
                        "message": {"role": "user", "content": text},
                    }
                )
                + "\n"
            ).encode()
        )
    frames: list[dict[str, Any]] = []
    pending = bytearray()
    output_bytes = 0
    terminals = 0
    closed_after_results = False
    expected_results = 1 if scenario == "tool-loop" else 2
    stderr_bytes = 0
    with owned_fixture_process(argv, environment, stdin=subprocess.PIPE) as process:
        assert process.stdin and process.stdout and process.stderr
        process.stdin.write(inputs[0])
        process.stdin.flush()
        sent = False
        deadline = time.monotonic() + 35
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map():
                if time.monotonic() >= deadline:
                    raise ValueError("example_native_input_timeout")
                if first.is_set() and not sent:
                    process.stdin.write(inputs[1])
                    process.stdin.flush()
                    sent = True
                    release.set()
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.fileobj is process.stdout:
                        output_bytes += len(chunk)
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, tail = pending.partition(b"\n")
                            pending = bytearray(tail)
                            frame = json.loads(line)
                            if not isinstance(frame, dict):
                                raise ValueError("example_native_input_invalid_frame")
                            frames.append(frame)
                            if frame.get("type") == "result":
                                terminals += 1
                                if terminals == expected_results:
                                    process.stdin.close()
                                    closed_after_results = True
                    else:
                        stderr_bytes += len(chunk)
                    if output_bytes > 2097152 or stderr_bytes > 65536:
                        raise ValueError("example_native_input_output_bound")
            while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                if time.monotonic() >= deadline:
                    raise ValueError("example_native_input_timeout")
                time.sleep(0.01)
        if not sent:
            raise ValueError("example_native_input_first_request_missing")
        if pending:
            raise ValueError("example_native_input_incomplete_frame")
    assert process.returncode is not None
    return process.returncode, frames, stderr_bytes, closed_after_results


def _make_server(
    scenario: str,
    first: threading.Event,
    release: threading.Event,
    requests: list[dict[str, Any]],
    counts: dict[str, int],
    lock: Any,
) -> ThreadingHTTPServer:
    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def get_request(self):
            connection, address = super().get_request()
            connection.settimeout(3)
            return connection, address

        def handle_error(self, request, client_address):
            with lock:
                counts["violations"] += 1

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def violation(self) -> None:
            with lock:
                counts["violations"] += 1

        def send_error(
            self, code: int, message: str | None = None, explain: str | None = None
        ) -> None:
            self.violation()
            super().send_error(code, message, explain)

        def respond(self, code, body=b"", content_type="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            with lock:
                counts["heads"] += 1
                valid = counts["heads"] <= 1 and self.path == "/api/hello"
            if not valid:
                self.violation()
            self.respond(200 if valid else 400)

        def do_POST(self):
            with lock:
                counts["posts"] += 1
                ordinal = counts["posts"]
            size = int(self.headers.get("Content-Length", "0"))
            if (
                ordinal > 2
                or not 0 < size <= 300000
                or self.path not in ("/v1/messages", "/v1/messages?beta=true")
            ):
                self.violation()
                self.respond(400)
                return
            body = json.loads(self.rfile.read(size))
            tools = {"Read"} if scenario == "tool-loop" else set()
            if (
                body.get("model") != "claude-opus-5-5"
                or body.get("stream") is not True
                or {t["name"] for t in body.get("tools", [])} != tools
            ):
                self.violation()
                self.respond(400)
                return
            with lock:
                requests.append(body)
            if ordinal == 1:
                first.set()
                if not release.wait(10):
                    self.violation()
                    self.respond(400)
                    return
            self.respond(
                200, _response(scenario == "tool-loop" and ordinal == 1), "text/event-stream"
            )

    return Server(("127.0.0.1", 0), Handler)


def _successful_read(request: dict[str, Any]) -> bool:
    for message in request.get("messages", []):
        content = message.get("content", [])
        if isinstance(content, list) and any(
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("is_error", False) is False
            and "example-fictional-seed" in json.dumps(block.get("content"))
            for block in content
        ):
            return True
    return False


def actor(scenario: str, sentinel: str, port: str) -> dict[str, Any]:
    sys.path.insert(0, "/opt/example")
    from native_process_capture import capture_owned_process

    first, release = threading.Event(), threading.Event()
    requests: list[dict[str, Any]] = []
    counts = {"posts": 0, "violations": 0, "heads": 0}
    lock = threading.Lock()

    with socket.socket() as probe:
        probe.settimeout(1)
        try:
            probe.connect(("127.0.0.1", int(port)))
            blocked = False
        except OSError:
            blocked = True
    server = _make_server(scenario, first, release, requests, counts, lock)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
    thread.start()
    environment = {
        "HOME": "/home/example",
        "CLAUDE_CONFIG_DIR": "/home/example/.claude",
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "ANTHROPIC_AUTH_TOKEN": "example-fake-credential",
        "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1",
        "CLAUDE_CODE_MAX_RETRIES": "0",
        "CLAUDE_CODE_NONSTREAMING_TIMEOUT_RETRIES": "0",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "1024",
    }
    argv = [
        "/opt/example/claude",
        "--setting-sources",
        "",
        "-p",
        "--output-format",
        "stream-json",
        "--input-format",
        "stream-json",
        "--replay-user-messages",
        "--verbose",
        "--restricted",
        "--safe-mode",
        "--settings",
        '{"disableAllHooks":true,"switchModelsOnFlag":false,"fallbackModel":[]}',
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--disable-slash-commands",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--tools",
        "Read" if scenario == "tool-loop" else "",
        "--model",
        "claude-opus-5-5",
        "--effort",
        "low",
        "--session-id",
        SESSION,
        "--system-prompt",
        "Process only the fictional test input.",
        "--no-session-persistence",
    ]
    try:
        code, version = capture_owned_process(
            [argv[0], "--version"], environment, timeout=10, stdout_limit=1024, stderr_limit=4096
        )
        if code != 0 or version.decode().strip() != "2.1.296 (Claude Code)":
            raise ValueError("example_native_input_version_mismatch")
        code, frames, stderr_bytes, open_until_results = _capture(
            argv, environment, first, release, scenario
        )
        terminals = [f for f in frames if f.get("type") == "result"]
        echoes = [f for f in frames if f.get("type") == "user" and f.get("uuid") in INPUT_IDS]
        boundary = next((i for i, f in enumerate(frames) if f.get("type") == "result"), len(frames))
        messages = [json.dumps(r.get("messages")) for r in requests]
        return {
            "scenario": scenario,
            "exit_code": code,
            "posts": counts["posts"],
            "violations": counts["violations"],
            "correction_in_requests": [CORRECTION in m for m in messages],
            "initial_in_requests": [INITIAL in m for m in messages],
            "tool_result_in_requests": ['"tool_result"' in m for m in messages],
            "successful_read_in_requests": [_successful_read(r) for r in requests],
            "stdin_open_until_results": open_until_results,
            "results": len(terminals),
            "successes": [
                r.get("subtype") == "success" and r.get("is_error") is False for r in terminals
            ],
            "echoed_ids": [e["uuid"] for e in echoes],
            "exact_echoes": len(echoes) == 2
            and all(
                e.get("message") == {"role": "user", "content": text}
                for e, text in zip(echoes, (INITIAL, CORRECTION), strict=True)
            ),
            "correction_before_first_result": any(
                f.get("uuid") == INPUT_IDS[1] for f in frames[:boundary]
            ),
            "same_session": all(f.get("session_id") == SESSION for f in terminals + echoes)
            and all(f["session_id"] == SESSION for f in frames if "session_id" in f),
            "stderr_bytes": stderr_bytes,
            "native_version": version.decode().strip(),
            "host_files_hidden": not Path(sentinel).exists(),
            "host_loopback_blocked": blocked,
        }
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(3)
        if thread.is_alive():
            raise ValueError("example_native_input_server_cleanup_failed")


if __name__ == "__main__":
    try:
        observation = actor(*sys.argv[1:])
        validate_observation(observation, sys.argv[1])
        print(json.dumps(observation))
    except Exception as error:
        print(json.dumps({"fixture_failed": True, "category": type(error).__name__}))
        raise SystemExit(1) from None
