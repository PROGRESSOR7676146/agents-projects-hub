"""Deterministic Responses fixture, reachable only inside an isolated namespace."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
import threading
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


plan = ResponsePlan()
lock = threading.Lock()
write_lock = threading.Lock()


def emit(value: dict) -> None:
    with write_lock:
        print(json.dumps(value), flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if self.path != "/v1/responses" or not 0 < length <= 2_000_000:
            self.send_error(400)
            return
        data = json.loads(self.rfile.read(length))
        with lock:
            plan.requests += 1
            number = plan.requests
            case = plan.case
            if number > 4 or data.get("model") != "example-offline":
                self.send_error(409)
                return
            emit({"fixture_event": "stub_request", "case": case, "sequence": number})
            if not plan.emitted:
                tools = [tool for tool in data.get("tools", []) if tool.get("type") == "function"]
                tool = next(
                    (
                        tool
                        for tool in tools
                        if tool.get("name") in ("exec_command", "shell_command", "shell")
                    ),
                    None,
                )
                if tool is None:
                    emit({"fixture_error": "execution_tool_absent"})
                    self.send_error(409)
                    return
                properties = tool.get("parameters", {}).get("properties", {})
                arguments: dict[str, Any]
                if "cmd" in properties:
                    arguments = {"cmd": shlex.join(COMMAND), "max_output_tokens": 1000}
                elif "command" in properties:
                    value = (
                        COMMAND
                        if properties["command"].get("type") == "array"
                        else shlex.join(COMMAND)
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
                plan.emitted = True
            else:
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


def launch_native(port: int) -> tuple[subprocess.Popen[str], list[threading.Thread]]:
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
        "mcp_servers={}",
    ]
    process = subprocess.Popen(
        ["codex", "app-server", *flags],
        executable=BINARY,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None

    def output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            emit(json.loads(line))

    def errors() -> None:
        assert process.stderr is not None
        for line in process.stderr:
            sys.stderr.write(line)

    threads = [
        threading.Thread(target=output, daemon=True),
        threading.Thread(target=errors, daemon=True),
    ]
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
    if (Path.home() / ".codex" / "auth.json").exists():
        raise RuntimeError("offline fixture must hide real authentication data")
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    process, threads = launch_native(port)
    emit({"fixture_event": "ready"})
    try:
        for line in sys.stdin:
            message = json.loads(line)
            if "fixture_case" in message:
                case = message["fixture_case"]
                if not isinstance(case, str) or re.fullmatch(r"example-case-[0-9]+", case) is None:
                    raise RuntimeError("invalid offline case")
                with lock:
                    plan.case, plan.requests, plan.emitted = case, 0, False
                emit({"fixture_event": "case_selected", "case": case})
            elif message.get("fixture_restart") is True:
                close_native(process, threads)
                process, threads = launch_native(port)
                emit({"fixture_event": "native_restarted"})
            else:
                assert process.stdin is not None
                process.stdin.write(line)
                process.stdin.flush()
    finally:
        close_native(process, threads)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
