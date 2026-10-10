"""Pinned text fresh/resume matrix, inside the owned offline namespace only."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, cast

if __name__ == "__main__":
    sys.path.insert(0, "/opt/example")

from tests.claude_image_request_contract import MISSING_SESSION_ID, validate_missing_session_result
from tests.claude_native_request_contract import (
    DUMMY,
    NATIVE_SESSION_ID,
    ExpectedNativeRequest,
    NativeRequestContractError,
    environment_text,
)
from tests.claude_native_transport_actor import FixtureServer, Handler
from tests.claude_selection_contract import (
    CASES,
    MARKERS,
    SelectionRequest,
    selection,
    validate_selection_body,
    validate_selection_headers,
)
from tests.native_process_capture import NativeCaptureError, capture_owned_process

COUNTERS = (
    "requests",
    "heads",
    "posts",
    "connections",
    "violations",
    "timeouts",
    "validated_requests",
    "messages_served",
)
STAGES = {"isolation", "version", "fresh", "resume", "missing_session", "cleanup", "complete"}
stage = "isolation"


class SelectionServer(FixtureServer):
    def __init__(self, host_port: int, expected: SelectionRequest, *, missing: bool = False):
        self.expected = expected
        self.missing = missing
        super().__init__(host_port, "api-key-success")
        self.RequestHandlerClass = SelectionHandler
        self.request_contract = ExpectedNativeRequest(
            "2.1.285", environment_text(expected.os_version, expected.date)
        )


class SelectionHandler(Handler):
    @property
    def fixture(self) -> SelectionServer:
        return cast(SelectionServer, self.server)

    def _post_headers(self) -> int:
        if self.fixture.missing:
            raise NativeRequestContractError("native_selection_missing_post")
        return validate_selection_headers(
            list(self.headers.raw_items()),
            port=self.fixture.server_port,
            expected=self.fixture.expected,
        )

    def _post_body(self, raw: bytes, expected: ExpectedNativeRequest) -> tuple[str, str]:
        validate_selection_body(raw, self.fixture.expected)
        model, _ = selection(self.fixture.expected.case, self.fixture.expected.phase)
        return model, MARKERS[self.fixture.expected.phase]


@contextmanager
def endpoint(
    host_port: int, expected: SelectionRequest, *, missing: bool = False
) -> Iterator[SelectionServer]:
    server = SelectionServer(host_port, expected, missing=missing)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.05))
    thread.start()
    try:
        yield server
    finally:
        global stage
        prior = stage
        stage = "cleanup"
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        if thread.is_alive():
            raise NativeCaptureError("native_fixture_listener_cleanup_failed")
        stage = prior


def counters(server: SelectionServer) -> dict[str, int]:
    return {key: getattr(server, key) for key in COUNTERS}


def main() -> None:
    from hermes_codex_router.claude_stream import ClaudeStreamReader, parse_claude_stream

    global stage
    host_port, sentinel, case = int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    invocations = json.loads(sys.argv[4])
    if (
        case not in CASES
        or not isinstance(invocations, list)
        or len(invocations) != 3
        or any(
            not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv)
            for argv in invocations
        )
    ):
        raise NativeCaptureError("native_selection_argv_invalid")
    hidden, blocked = not sentinel.exists(), False
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
    os_version, date = "Linux " + os.uname().release, datetime.now(timezone.utc).date().isoformat()
    phases = []
    ports = []
    store = Path("/home/example/.claude/projects")
    for phase in (0, 1):
        stage = "fresh" if phase == 0 else "resume"
        expected = SelectionRequest(case, phase, os_version, date)
        model, effort = selection(case, phase)
        with endpoint(host_port, expected) as server:
            environment["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:" + str(server.server_port)
            visible = []
            reader = ClaudeStreamReader(
                expected_session_id=NATIVE_SESSION_ID, on_visible_assistant=visible.append
            )
            code, _ = capture_owned_process(
                invocations[phase],
                environment,
                timeout=40,
                stdout_limit=2 * 1024 * 1024,
                stderr_limit=65536,
                on_stdout=reader.feed,
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
                or init[0].get("model") != model
            ):
                raise NativeCaptureError("native_fixture_init_policy_unproven")
            result = parse_claude_stream(
                output,
                expected_session_id=NATIVE_SESSION_ID,
                requested_model=model,
                returncode=code,
            )
            if (
                code != 0
                or result.text != MARKERS[phase]
                or result.model != model
                or len(visible) != 1
                or visible[0].text != MARKERS[phase]
            ):
                raise NativeCaptureError("native_fixture_completion_unproven")
        ports.append(server.server_port)
        saved = sorted(store.rglob("*.jsonl"))
        if (
            len(saved) != 1
            or saved[0].name != NATIVE_SESSION_ID + ".jsonl"
            or not 0 < saved[0].stat().st_size <= 1024 * 1024
        ):
            raise NativeCaptureError("native_fixture_session_store_unproven")
        phases.append(
            {
                "phase": phase,
                "model": model,
                "effort": effort,
                "session_id": result.session_id,
                "completed": True,
                "store_exact": True,
                **counters(server),
            }
        )
    stage = "missing_session"
    before = hashlib.sha256(saved[0].read_bytes()).hexdigest()
    with endpoint(host_port, SelectionRequest(case, 1, os_version, date), missing=True) as server:
        environment["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:" + str(server.server_port)
        code, output = capture_owned_process(
            invocations[2], environment, timeout=15, stdout_limit=65536, stderr_limit=65536
        )
    validate_missing_session_result(output, returncode=code)
    ports.append(server.server_port)
    unchanged = (
        sorted(store.rglob("*.jsonl")) == saved
        and 0 < saved[0].stat().st_size <= 1024 * 1024
        and hashlib.sha256(saved[0].read_bytes()).hexdigest() == before
    )
    if any(path.name == MISSING_SESSION_ID + ".jsonl" for path in store.rglob("*.jsonl")):
        raise NativeCaptureError("native_fixture_replacement_session")
    stage = "complete"
    print(
        json.dumps(
            {
                "case": case,
                "native_version": version.decode("ascii").strip(),
                "host_files_hidden": hidden,
                "host_loopback_blocked": blocked,
                "ports_distinct": all(port != host_port for port in ports),
                "phases": phases,
                "missing": {
                    "refusal_validated": True,
                    "store_unchanged": unchanged,
                    "exit_code": code,
                    **counters(server),
                },
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        print(json.dumps({"fixture_failed": True, "stage": stage}), flush=True)
        raise SystemExit(1)
