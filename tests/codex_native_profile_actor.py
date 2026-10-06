"""Deterministic Responses fixture, reachable only inside an isolated namespace."""

from __future__ import annotations

import json
import os
import re
import runpy
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

BINARY = "/usr/local/bin/example-codex"
PROBE = """
import json,pathlib
p=pathlib.Path.cwd(); key=p.parent/'example-authority.key'; result={}
for name,path in [('project_read',p/'visible'),('authority_read',key),
                  ('authority_symlink_read',p/'private-link')]:
    try: path.read_bytes(); result[name]=True
    except OSError: result[name]=False
for name,path in [('project_write',p/'example-write'),('git_write',p/'.git'/'HEAD')]:
    try: path.write_text('example mutation'); result[name]=True
    except OSError: result[name]=False
print('EXAMPLE_PROBE:'+json.dumps(result))
"""
COMMAND = ["/usr/bin/python3", "-I", "-c", PROBE]


@dataclass
class ResponsePlan:
    case: str = "example-initial"
    requests: int = 0
    emitted: bool = False
    kind: str = "command"
    nonce: str | None = None


plan = ResponsePlan()
lock = threading.Lock()
write_lock = threading.Lock()
total_requests = 0
mcp_fixture: dict[str, Any] | None = None
notification_burst = threading.Event()
notification_finish = threading.Event()


def emit(value: dict) -> None:
    with write_lock:
        print(json.dumps(value), flush=True)


def select_tool(tools: list[dict], kind: str, server: str | None) -> tuple[dict, str | None]:
    matches: list[tuple[dict, str | None]] = []
    namespace = "mcp__" + server.replace("-", "_") if server is not None else None
    for tool in tools:
        if tool.get("type") == "function":
            names = (
                {namespace + "__probe"}
                if kind == "mcp" and namespace is not None
                else {"exec_command", "shell_command", "shell"}
            )
            if tool.get("name") in names:
                matches.append((tool, None))
        elif kind == "mcp" and tool.get("type") == "namespace" and tool.get("name") == namespace:
            for function in tool.get("tools", []):
                if function.get("type") == "function" and function.get("name") == "probe":
                    matches.append((function, namespace))
    if len(matches) != 1:
        raise ValueError("fixed execution tool is absent or ambiguous")
    return matches[0]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        global total_requests
        length = int(self.headers.get("Content-Length", "0"))
        if self.path != "/v1/responses" or not 0 < length <= 2_000_000:
            self.send_error(400)
            return
        data = json.loads(self.rfile.read(length))
        if plan.kind == "notifications":
            with lock:
                plan.requests += 1
                total_requests += 1
                if plan.requests != 1 or data.get("model") != "example-offline":
                    self.send_error(409)
                    return
                case = plan.case
            self.send_notifications(case)
            return
        with lock:
            plan.requests += 1
            total_requests += 1
            number = plan.requests
            case = plan.case
            if number > 4 or data.get("model") != "example-offline":
                self.send_error(409)
                return
            emit({"fixture_event": "stub_request", "case": case, "sequence": number})
            if not plan.emitted:
                try:
                    tool, namespace = select_tool(
                        data.get("tools", []),
                        plan.kind,
                        mcp_fixture["SERVER"] if mcp_fixture is not None else None,
                    )
                except ValueError:
                    emit(
                        {
                            "fixture_event": "tool_inventory",
                            "tool_inventory": [
                                {"type": tool.get("type"), "name": tool.get("name")}
                                for tool in data.get("tools", [])
                            ],
                        }
                    )
                    emit({"fixture_error": "execution_tool_absent"})
                    self.send_error(409)
                    return
                properties = tool.get("parameters", {}).get("properties", {})
                arguments: dict[str, Any]
                command = COMMAND
                if plan.kind == "custody_command" and mcp_fixture is not None:
                    source = mcp_fixture["PROBE_SOURCE"].replace(
                        "print(json.dumps(result))", 'print("EXAMPLE_CUSTODY:"+json.dumps(result))'
                    )
                    command = ["/usr/bin/python3", "-I", "-c", source]
                if plan.kind == "mcp":
                    if set(properties) != {"nonce"}:
                        emit({"fixture_error": "mcp_tool_schema_unknown"})
                        self.send_error(409)
                        return
                    arguments = {"nonce": plan.nonce}
                elif "cmd" in properties:
                    arguments = {"cmd": shlex.join(command), "max_output_tokens": 1000}
                elif "command" in properties:
                    value = (
                        command
                        if properties["command"].get("type") == "array"
                        else shlex.join(command)
                    )
                    arguments = {"command": value}
                else:
                    emit({"fixture_error": "execution_tool_schema_unknown"})
                    self.send_error(409)
                    return
                if "login" in properties:
                    arguments["login"] = False
                if "sandbox_permissions" in properties:
                    arguments["sandbox_permissions"] = "use_default"
                output = {
                    "type": "function_call",
                    "id": "fc_" + case,
                    "call_id": "call_" + case,
                    "name": tool["name"],
                    "arguments": json.dumps(arguments),
                }
                if namespace is not None:
                    output["namespace"] = namespace
                plan.emitted = True
            else:
                matches = [
                    item
                    for item in data.get("input", [])
                    if item.get("type") == "function_call_output"
                    and item.get("call_id") == "call_" + case
                    and isinstance(item.get("output"), (str, list))
                ]
                if len(matches) != 1:
                    emit({"fixture_error": "matching_function_output_absent"})
                    self.send_error(409)
                    return
                output = {
                    "type": "message",
                    "id": "msg_" + case,
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Example fixture complete.",
                            "annotations": [],
                        }
                    ],
                }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        response = {
            "id": "resp_" + case + "_" + str(number),
            "object": "response",
            "created_at": 1,
            "status": "in_progress",
            "model": "example-offline",
            "output": [],
        }
        events = [
            {"type": "response.created", "response": response},
            {"type": "response.output_item.added", "output_index": 0, "item": output},
            {"type": "response.output_item.done", "output_index": 0, "item": output},
            {
                "type": "response.completed",
                "response": {
                    **response,
                    "status": "completed",
                    "output": [output],
                    "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                },
            },
        ]
        try:
            for sequence, event in enumerate(events):
                self.wfile.write(
                    (
                        "event: "
                        + event["type"]
                        + "\ndata: "
                        + json.dumps({**event, "sequence_number": sequence})
                        + "\n\n"
                    ).encode()
                )
            self.wfile.flush()
        except BrokenPipeError:
            return  # The native client may close a stream after its terminal event.

    def send_notifications(self, case: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        response = {
            "id": "resp_" + case,
            "object": "response",
            "created_at": 1,
            "status": "in_progress",
            "model": "example-offline",
            "output": [],
        }
        item = {
            "type": "message",
            "id": "msg_" + case,
            "status": "in_progress",
            "role": "assistant",
            "phase": "final_answer",
            "content": [],
        }
        part = {"type": "output_text", "text": "", "annotations": []}
        sequence = 0

        def send(event: dict) -> None:
            nonlocal sequence
            self.wfile.write(
                (
                    "event: "
                    + event["type"]
                    + "\ndata: "
                    + json.dumps({**event, "sequence_number": sequence})
                    + "\n\n"
                ).encode()
            )
            self.wfile.flush()
            sequence += 1

        try:
            send({"type": "response.created", "response": response})
            send({"type": "response.output_item.added", "output_index": 0, "item": item})
            send(
                {
                    "type": "response.content_part.added",
                    "output_index": 0,
                    "content_index": 0,
                    "item_id": item["id"],
                    "part": part,
                }
            )
            delta = {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "item_id": item["id"],
                "delta": "x",
            }
            send(delta)
            emit({"fixture_event": "stream_waiting", "case": case})
            if not notification_burst.wait(30):
                raise RuntimeError("offline notification burst deadline")
            for _ in range(1200):
                send(delta)
            emit({"fixture_event": "burst_sent", "case": case})
            if not notification_finish.wait(30):
                raise RuntimeError("offline notification completion deadline")
            content = {**part, "text": "x" * 1201}
            completed = {**item, "status": "completed", "content": [content]}
            send(
                {
                    "type": "response.output_text.done",
                    "output_index": 0,
                    "content_index": 0,
                    "item_id": item["id"],
                    "text": content["text"],
                }
            )
            send(
                {
                    "type": "response.content_part.done",
                    "output_index": 0,
                    "content_index": 0,
                    "item_id": item["id"],
                    "part": content,
                }
            )
            send({"type": "response.output_item.done", "output_index": 0, "item": completed})
            send(
                {
                    "type": "response.completed",
                    "response": {
                        **response,
                        "status": "completed",
                        "output": [completed],
                        "usage": {"input_tokens": 0, "output_tokens": 1201, "total_tokens": 1201},
                    },
                }
            )
        except BrokenPipeError:
            return


def launch_native(
    port: int, *, listener: Path | None = None
) -> tuple[subprocess.Popen[str], list[threading.Thread]]:
    mcp_config = "mcp_servers={}"
    if mcp_fixture is not None:
        root = mcp_fixture["trusted_root"]()
        mcp_config = (
            f"mcp_servers.{mcp_fixture['SERVER']}="
            + '{command="/usr/bin/python3",args=["-I","/opt/example-native/mcp_server.py"],'
            + f"cwd={json.dumps(str(root))},env={{EXAMPLE_MCP_PROJECT={json.dumps(str(root))}}}"
            + "}"
        )
    flags = [
        "-c",
        'model="example-offline"',
        "-c",
        'model_provider="example-offline"',
        "-c",
        f'model_providers.example-offline={{name="Example offline",base_url="http://127.0.0.1:{port}/v1",wire_api="responses",requires_openai_auth=false,request_max_retries=0,stream_max_retries=0,stream_idle_timeout_ms=10000}}',
        "-c",
        'web_search="disabled"',
        "-c",
        "features.apps=false",
        "-c",
        "features.remote_models=false",
        "-c",
        "features.shell_snapshot=false",
        "-c",
        "analytics.enabled=false",
        "-c",
        mcp_config,
    ]
    process = subprocess.Popen(
        [
            "codex",
            "app-server",
            *flags,
            *(["--listen", "unix://" + str(listener)] if listener else []),
        ],
        executable=BINARY,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL if listener else subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stderr is not None

    def output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            emit(json.loads(line))

    def errors() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            sys.stderr.write(line)

    threads = [threading.Thread(target=errors, daemon=True)]
    if listener is None:
        threads.append(threading.Thread(target=output, daemon=True))
    for thread in threads:
        thread.start()
    return process, threads


def close_native(process: subprocess.Popen[str], threads: list[threading.Thread]) -> None:
    assert process.stdin is not None
    try:
        process.stdin.close()
    except BrokenPipeError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=2)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def main() -> None:
    global mcp_fixture
    if sys.argv[1:] == ["--mcp"]:
        mcp_fixture = runpy.run_path("/opt/example-native/mcp_server.py")
        # This is trusted fixture setup, never a tool-selected root.
        os.environ["EXAMPLE_MCP_PROJECT"] = str(Path.cwd())
    elif sys.argv[1:] not in ([], ["--notifications"]):
        raise RuntimeError("invalid offline fixture arguments")
    if (Path.home() / ".codex" / "auth.json").exists():
        raise RuntimeError("offline fixture must hide real authentication data")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    listener = Path.cwd() / "example-native.sock" if sys.argv[1:] == ["--notifications"] else None
    process, threads = launch_native(port, listener=listener)
    if listener is not None:
        deadline = time.monotonic() + 15
        while not listener.exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                close_native(process, threads)
                raise RuntimeError("offline native listener unavailable")
            threading.Event().wait(0.01)
    emit({"fixture_event": "ready", "listener_mode": listener is not None})
    try:
        for line in sys.stdin:
            message = json.loads(line)
            if "fixture_case" in message:
                case = message["fixture_case"]
                if not isinstance(case, str) or re.fullmatch(r"example-case-[0-9]+", case) is None:
                    raise RuntimeError("invalid offline case")
                with lock:
                    plan.case, plan.requests, plan.emitted = case, 0, False
                    plan.kind = message.get("fixture_kind", "command")
                    plan.nonce = message.get("fixture_nonce")
                    if plan.kind not in ("command", "mcp", "custody_command", "notifications") or (
                        plan.kind == "mcp"
                        and (mcp_fixture is None or not mcp_fixture["valid_nonce"](plan.nonce))
                    ):
                        raise RuntimeError("invalid offline fixture kind")
                    if plan.kind == "custody_command" and mcp_fixture is None:
                        raise RuntimeError("custody command requires fixed MCP fixture")
                    if plan.kind == "notifications" and listener is None:
                        raise RuntimeError("notification case requires native listener")
                    notification_burst.clear()
                    notification_finish.clear()
                emit({"fixture_event": "case_selected", "case": case})
            elif message.get("fixture_burst") is True and listener is not None:
                notification_burst.set()
            elif message.get("fixture_finish") is True and listener is not None:
                notification_finish.set()
            elif message.get("fixture_stats") is True:
                with lock:
                    emit({"fixture_event": "stats", "responses_requests": total_requests})
            elif message.get("fixture_restart") is True:
                if listener is not None:
                    raise RuntimeError("listener restart unsupported")
                close_native(process, threads)
                process, threads = launch_native(port, listener=listener)
                emit({"fixture_event": "native_restarted"})
            else:
                if listener is not None:
                    raise RuntimeError("listener fixture control pipe cannot forward RPC")
                assert process.stdin is not None
                process.stdin.write(line)
                process.stdin.flush()
    finally:
        notification_burst.set()
        notification_finish.set()
        close_native(process, threads)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
