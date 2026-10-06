"""Explicitly opted-in native fixture; no real login, inference, or host network."""

from __future__ import annotations

import json
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from tests.codex_native_mcp_consent import SyntheticMcpConsent
from tests.codex_native_namespace import native_namespace_argv, native_namespace_environment
from tests.codex_native_profile_actor import COMMAND

PROFILE_ID = "example-managed-custody"
PROBE_KEYS = {
    "project_read",
    "project_write",
    "authority_read",
    "authority_symlink_read",
    "git_write",
}


class NativeProfileFixtureError(RuntimeError):
    """Fixed fixture diagnostics never include native payloads or credentials."""


class NativeProfileTransport:
    """Use the actual Hub adapter with the same isolated offline native actor."""

    def __init__(self, fixture: NativeProfileFixture) -> None:
        self.fixture = fixture

    def send(self, message: dict[str, Any]) -> None:
        self.fixture._send(message)

    def receive(self, timeout: float | None = None) -> dict[str, Any]:
        return self.fixture._take(time.monotonic() + (30 if timeout is None else timeout))

    def close(self) -> None:
        # The fixture owns process cleanup and optional app-server restart.
        pass


def parse_probe(output: str) -> dict[str, bool]:
    matches = re.findall(r"EXAMPLE_PROBE:(\{[^\n]*\})", output)
    if output.count("EXAMPLE_PROBE:") != 1 or len(matches) != 1:
        raise NativeProfileFixtureError("probe_output_not_unique")
    try:
        result = json.loads(matches[0])
    except json.JSONDecodeError as error:
        raise NativeProfileFixtureError("probe_output_json_invalid") from error
    if not isinstance(result, dict) or set(result) != PROBE_KEYS:
        raise NativeProfileFixtureError("probe_output_shape_invalid")
    if not all(isinstance(value, bool) for value in result.values()):
        raise NativeProfileFixtureError("probe_output_types_invalid")
    return result


def proven_probe(result: dict[str, Any]) -> dict[str, bool]:
    thread_id, turn_id = result.get("thread_id"), result.get("turn_id")
    if result.get("status") != "completed" or not all(
        isinstance(value, str) and value for value in (thread_id, turn_id)
    ):
        raise NativeProfileFixtureError("current_turn_not_completed")
    matches = [
        row["item"]
        for row in result.get("items", [])
        if row.get("thread_id") == thread_id
        and row.get("turn_id") == turn_id
        and row.get("item", {}).get("type") == "commandExecution"
    ]
    if len(matches) != 1:
        raise NativeProfileFixtureError("current_execution_not_unique")
    item = matches[0]
    code = item.get("exitCode")
    if item.get("status") != "completed" or type(code) is not int or code != 0:
        raise NativeProfileFixtureError("current_execution_not_successful")
    return parse_probe(item.get("aggregatedOutput", ""))


class NativeProfileFixture(AbstractContextManager["NativeProfileFixture"]):
    def __init__(self, executable: Path, *, mcp: bool = False) -> None:
        self.executable = executable.resolve(strict=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="example-native-profile-")
        self.base = Path(self.temporary.name)
        self.project = self.base / "example-project"
        self.process: subprocess.Popen[str] | None = None
        self.output_thread: threading.Thread | None = None
        self.error_thread: threading.Thread | None = None
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=512)
        self.events: list[dict[str, Any]] = []
        self.reader_error: str | None = None
        self.next_id = 0
        self.case_id = 0
        self.mcp = mcp
        self.synthetic_consent: SyntheticMcpConsent | None = None
        self.pending_mcp_request: dict | None = None

    def __enter__(self) -> NativeProfileFixture:
        try:
            self._start()
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def _start(self) -> None:
        bwrap = Path("/usr/bin/bwrap")
        if not bwrap.is_file() or not self.executable.is_file():
            raise NativeProfileFixtureError("explicit_native_fixture_unavailable")
        snapshot = self.base / "example-codex-binary"
        shutil.copyfile(self.executable, snapshot)
        snapshot.chmod(0o500)
        self.project.mkdir()
        subprocess.run(
            ["git", "init", "--quiet", "--initial-branch=example", str(self.project)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (self.project / "visible").write_text("example visible project data")
        key = self.base / "example-authority.key"
        key.write_text("fictional sentinel; no real credential")
        key.chmod(0o600)
        (self.project / "private-link").symlink_to(key)
        requirements = self.base / "example-requirements.toml"
        requirements.write_text(f'''default_permissions = "{PROFILE_ID}"
allowed_approval_policies = ["on-request", "never"]
allowed_approvals_reviewers = ["user"]
[allowed_permission_profiles]
{PROFILE_ID} = true
[permissions.{PROFILE_ID}]
extends = ":workspace"
[permissions.{PROFILE_ID}.filesystem]
":root" = "deny"
":minimal" = "read"
":slash_tmp" = "deny"
":tmpdir" = "deny"
"/usr/local/bin/example-codex" = "read"
[permissions.{PROFILE_ID}.network]
enabled = false
''')
        actor = Path(__file__).with_name("codex_native_profile_actor.py").resolve(strict=True)
        argv = native_namespace_argv(
            binary=snapshot,
            actor=actor,
            requirements=requirements,
            project=self.project,
            authority=key,
            mcp_server=actor.with_name("codex_native_mcp_server.py") if self.mcp else None,
        )
        self.process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=native_namespace_environment(),
        )
        assert self.process.stdout is not None and self.process.stderr is not None
        self.output_thread = threading.Thread(target=self._read_output, daemon=True)
        self.error_thread = threading.Thread(target=self._read_errors, daemon=True)
        self.output_thread.start()
        self.error_thread.start()
        deadline = time.monotonic() + 15
        while self._take(deadline).get("fixture_event") != "ready":
            pass
        self._initialize()

    def _initialize(self) -> None:
        self.rpc(
            "initialize",
            {
                "clientInfo": {"name": "example-profile-fixture", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        self._send({"method": "initialized", "params": {}})

    def restart_native(self) -> None:
        """Restart only the disposable app-server, retaining its private state."""
        self._clear_consent()
        self._send({"fixture_restart": True})
        deadline = time.monotonic() + 30
        while self._take(deadline).get("fixture_event") != "native_restarted":
            pass
        self._initialize()

    def _read_output(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        try:
            while line := self.process.stdout.readline(2_000_001):
                if len(line) > 2_000_000:
                    raise NativeProfileFixtureError("native_fixture_line_bound")
                self.messages.put_nowait(json.loads(line))
        except (ValueError, queue.Full, NativeProfileFixtureError):
            self.reader_error = "native_fixture_stream_invalid"
        finally:
            if self.reader_error is None:
                self.reader_error = "native_fixture_exited"

    def _read_errors(self) -> None:
        assert self.process is not None and self.process.stderr is not None
        with (self.base / "private-diagnostics.txt").open("w") as output:
            remaining = 8192
            while line := self.process.stderr.readline(8193):
                output.write(line[:remaining])
                remaining = max(0, remaining - len(line))

    def _send(self, value: dict[str, Any]) -> None:
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write(json.dumps(value) + "\n")
        self.process.stdin.flush()

    def _take(self, deadline: float) -> dict[str, Any]:
        if time.monotonic() >= deadline:
            raise NativeProfileFixtureError("native_fixture_deadline")
        if self.reader_error and self.messages.empty():
            raise NativeProfileFixtureError(self.reader_error)
        try:
            message = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
        except queue.Empty as error:
            raise NativeProfileFixtureError("native_fixture_deadline") from error
        if "fixture_error" in message:
            raise NativeProfileFixtureError("offline_provider_fixture_failed")
        if len(self.events) >= 4096:
            raise NativeProfileFixtureError("native_fixture_event_bound")
        self.events.append(message)
        if "id" in message and "method" in message:
            if message["method"] in (
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            ):
                self._send({"id": message["id"], "result": {"decision": "decline"}})
            elif message["method"] == "item/permissions/requestApproval":
                self._send({"id": message["id"], "result": {"permissions": {}, "scope": "turn"}})
            elif message["method"] == "mcpServer/elicitation/request":
                if self.pending_mcp_request is not None:
                    self._clear_consent()
                answer = (
                    self.synthetic_consent.answer(message, self.events)
                    if self.synthetic_consent is not None
                    else {"action": "decline"}
                )
                if answer is None:
                    self.pending_mcp_request = message
                else:
                    self._answer_mcp(message, answer)
            else:
                self._send(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "offline fixture denies client tool requests",
                        },
                    }
                )
        return message

    def _clear_consent(self) -> None:
        self.synthetic_consent = None
        request, self.pending_mcp_request = self.pending_mcp_request, None
        if request is not None:
            try:
                self._answer_mcp(request, {"action": "decline"})
            except OSError:
                pass  # Disposal still owns process cleanup after a dead fixture pipe.

    def _answer_mcp(self, request: dict, answer: dict) -> None:
        self._send({"id": request["id"], "result": answer})
        params = request.get("params", {})
        self.events.append(
            {
                "fixture_event": "mcp_consent_decision",
                "request_id": request["id"],
                "thread_id": params.get("threadId"),
                "turn_id": params.get("turnId"),
                "action": answer["action"],
            }
        )

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.next_id += 1
        identifier = self.next_id
        self._send({"id": identifier, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            message = self._take(deadline)
            if message.get("id") == identifier and "method" not in message:
                if "error" in message:
                    raise NativeProfileFixtureError("native_fixture_rpc_rejected")
                return message["result"]
        raise NativeProfileFixtureError("native_fixture_rpc_deadline")

    def start_thread(self) -> dict[str, Any]:
        return self.rpc(
            "thread/start",
            {
                "cwd": str(self.project),
                "model": "example-offline",
                "modelProvider": "example-offline",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "permissions": PROFILE_ID,
                "ephemeral": False,
            },
        )

    def resume_thread(self, thread_id: str) -> dict[str, Any]:
        return self.rpc(
            "thread/resume",
            {
                "threadId": thread_id,
                "cwd": str(self.project),
                "model": "example-offline",
                "modelProvider": "example-offline",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                "permissions": PROFILE_ID,
                "excludeTurns": True,
            },
        )

    def prepare_turn_case(self, *, kind: str = "command", nonce: str | None = None) -> int:
        self.case_id += 1
        case = f"example-case-{self.case_id}"
        (self.project / ".git" / "HEAD").write_text("ref: refs/heads/example\n")
        # Reset content in place; the namespace retains this exact mounted inode.
        (self.base / "example-authority.key").write_text("fictional sentinel; no real credential")
        self._send({"fixture_case": case, "fixture_kind": kind, "fixture_nonce": nonce})
        deadline = time.monotonic() + 30
        while self._take(deadline).get("case") != case:
            pass
        return len(self.events)

    def turn(
        self,
        thread_id: str,
        *,
        legacy: bool = False,
        kind: str = "command",
        nonce: str | None = None,
        synthetic_consent: bool = False,
    ) -> dict[str, Any]:
        offset = self.prepare_turn_case(kind=kind, nonce=nonce)
        self._clear_consent()
        if synthetic_consent:
            if not self.mcp or kind != "mcp" or nonce is None:
                raise NativeProfileFixtureError("synthetic_consent_requires_fixed_mcp_case")
            self.synthetic_consent = SyntheticMcpConsent(thread_id, nonce)
        deadline = time.monotonic() + 30
        policy = (
            {
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": [str(self.project)],
                    "networkAccess": False,
                }
            }
            if legacy
            else {"permissions": PROFILE_ID}
        )
        try:
            return self._run_turn(thread_id, offset, deadline, policy)
        finally:
            self._clear_consent()

    def _run_turn(
        self, thread_id: str, offset: int, deadline: float, policy: dict
    ) -> dict[str, Any]:
        started = self.rpc(
            "turn/start",
            {
                "threadId": thread_id,
                "cwd": str(self.project),
                "input": [
                    {
                        "type": "text",
                        "text": "Example offline fixture. Run the single provided diagnostic tool call.",
                    }
                ],
                "model": "example-offline",
                "effort": "low",
                "approvalPolicy": "on-request",
                "approvalsReviewer": "user",
                **policy,
            },
        )
        turn_id = started["turn"]["id"]
        if self.synthetic_consent is not None:
            self.synthetic_consent.turn_id = turn_id
            if self.pending_mcp_request is not None:
                request = self.pending_mcp_request
                self.pending_mcp_request = None
                answer = self.synthetic_consent.answer(request, self.events)
                assert answer is not None
                self._answer_mcp(request, answer)
        while not any(
            message.get("method") == "turn/completed"
            and message.get("params", {}).get("threadId") == thread_id
            and message.get("params", {}).get("turn", {}).get("id") == turn_id
            for message in self.events[offset:]
        ):
            self._take(deadline)
        return self.turn_evidence(thread_id, turn_id, offset)

    def responses_count(self) -> int:
        self._send({"fixture_stats": True})
        deadline = time.monotonic() + 30
        while True:
            message = self._take(deadline)
            if message.get("fixture_event") == "stats":
                return message["responses_requests"]

    def turn_evidence(self, thread_id: str, turn_id: str, offset: int) -> dict[str, Any]:
        messages = self.events[offset:]
        terminal = next(
            message["params"]["turn"]
            for message in messages
            if message.get("method") == "turn/completed"
            and message.get("params", {}).get("threadId") == thread_id
            and message.get("params", {}).get("turn", {}).get("id") == turn_id
        )
        return {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "status": terminal["status"],
            "items": [
                {
                    "thread_id": message["params"].get("threadId"),
                    "turn_id": message["params"].get("turnId"),
                    "item": message["params"]["item"],
                }
                for message in messages
                if message.get("method") == "item/completed"
            ],
            "settings": [
                message["params"].get("threadSettings", {})
                for message in messages
                if message.get("method") == "thread/settings/updated"
                and message["params"].get("threadId") == thread_id
            ],
        }

    def command_probe(self, *, legacy: bool = False) -> dict[str, Any]:
        policy = (
            {
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": [str(self.project)],
                    "networkAccess": False,
                }
            }
            if legacy
            else {}
        )
        result = self.rpc(
            "command/exec",
            {"command": COMMAND, "cwd": str(self.project), "timeoutMs": 5000, **policy},
        )
        return {**result, "probe": parse_probe(result.get("stdout", ""))}

    def __exit__(self, *args: object) -> None:
        if self.process is not None:
            self._clear_consent()
            if self.process.stdin is not None:
                try:
                    self.process.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            for thread in (self.output_thread, self.error_thread):
                if thread is not None:
                    thread.join(timeout=2)
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()
        self.temporary.cleanup()
