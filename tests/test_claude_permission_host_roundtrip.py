"""Offline native-hook to human-receipt round trips over real Unix sockets."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from hermes_codex_router.claude_file_sandbox import FileToolSandboxConfig, FileToolSandboxError
from hermes_codex_router.claude_permission_hook import run_hook
from hermes_codex_router.claude_permission_host import PermissionServer
from hermes_codex_router.claude_permission_protocol import ProtectedPayload, event_digest
from hermes_codex_router.claude_permissions_journal import ClaudePermissionJournal, PermissionLaunch
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.state import HubState
from hermes_codex_router.tlive_permissions import ProtectedTliveClient, TlivePermissionConfig
from tests.namespace_fixture import namespace_unavailable


def _mac(key: bytes, fields: list[object]) -> str:
    wire = json.dumps(fields, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hmac.new(key, wire.encode(), hashlib.sha256).hexdigest()


def _git_root(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True, timeout=5)


class FakeTlive:
    """A socket peer that validates Hub's HMAC before emitting signed receipts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.request_key = b"r" * 32
        self.result_key = b"s" * 32
        self.epoch = str(uuid4())
        self.received = threading.Event()
        self.release = threading.Event()
        self.stop = threading.Event()
        self.requests: list[dict[str, object]] = []
        self.decision = "allow"
        self.actor_change: dict[str, object] | None = None
        self.actor_none = False
        self.result_change: dict[str, object] | None = None
        self.drop = False
        self.errors: list[BaseException] = []
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.listener.bind(str(path))
        except OSError:
            self.listener.close()
            raise
        path.chmod(0o600)
        self.listener.listen(4)
        self.listener.settimeout(0.2)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @staticmethod
    def _read(conn: socket.socket) -> dict[str, object]:
        conn.settimeout(3)
        data = bytearray()
        while b"\n" not in data:
            part = conn.recv(4096)
            if not part or len(data) + len(part) > 70000:
                raise ValueError("incomplete protected request")
            data.extend(part)
        return json.loads(data.split(b"\n", 1)[0])

    def _serve(self) -> None:
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                with conn:
                    request = self._read(conn)
                    if request["kind"] == "hub.permission.hello":
                        challenge = request["challenge"]
                        reply = {
                            "kind": "hub.permission.capability",
                            "version": 1,
                            "epoch": self.epoch,
                            "tag": _mac(self.result_key, ["capability", challenge, self.epoch, 1]),
                        }
                    else:
                        payload = request["payload"]
                        assert request["kind"] == "hub.permission.request"
                        assert request["epoch"] == self.epoch
                        assert request["tag"] == _mac(
                            self.request_key, ["request", self.epoch, payload]
                        )
                        self.requests.append(request)
                        self.received.set()
                        if not self.release.wait(3) or self.drop:
                            continue
                        binding = ProtectedPayload.parse(str(payload))
                        suffix = "a" if self.decision == "allow" else "d"
                        actor: dict[str, object] | None = {
                            "callbackId": "example-callback",
                            "userId": "42",
                            "isBot": False,
                            "chatId": "42",
                            "chatType": "private",
                            "messageId": "123",
                            "data": f"hp:{binding.request_nonce}:{suffix}",
                        }
                        if self.actor_change is not None:
                            actor = {**actor, **self.actor_change} if actor is not None else None
                        if self.actor_none:
                            actor = None
                        reply = {
                            "kind": "hub.permission.result",
                            "decision": self.decision,
                            "epoch": self.epoch,
                            "payload": payload,
                            "actor": actor,
                        }
                        reply["tag"] = _mac(
                            self.result_key, ["result", self.epoch, payload, self.decision, actor]
                        )
                        if self.result_change:
                            reply.update(self.result_change)
                    conn.sendall(json.dumps(reply, separators=(",", ":")).encode() + b"\n")
            except OSError:
                pass  # The host may close its in-flight protected connection.
            except (ValueError, AssertionError, KeyError) as exc:
                self.errors.append(exc)

    def close(self) -> None:
        self.stop.set()
        self.release.set()
        self.listener.close()
        self.thread.join(4)
        if self.thread.is_alive():
            raise AssertionError("fake tlive did not stop")


class PermissionHostPeerGateTests(unittest.TestCase):
    def test_untrusted_accepted_connections_close_before_worker_thread(self) -> None:
        from hermes_codex_router import unix_peer

        class FakeConnection:
            closed = False

            def close(self) -> None:
                self.closed = True

        class FakeListener:
            def __init__(self, connection: FakeConnection) -> None:
                self.connection = connection
                self.calls = 0

            def accept(self):
                self.calls += 1
                if self.calls == 1:
                    return self.connection, None
                raise OSError("listener stopped")

        for result in ((1, os.geteuid() + 1, 0), OSError("unavailable")):
            with self.subTest(result=result):
                connection = FakeConnection()
                server = PermissionServer.__new__(PermissionServer)
                server.stop = threading.Event()
                server.inflight = None
                server.listener = FakeListener(connection)
                options = (
                    {"return_value": result}
                    if isinstance(result, tuple)
                    else {"side_effect": result}
                )
                with (
                    patch.object(unix_peer, "_peer_credentials", **options),
                    patch.object(PermissionServer, "_serve_connection") as serve_connection,
                    patch.object(HubState, "open") as db_open,
                ):
                    server._serve()
                    serve_connection.assert_not_called()
                    db_open.assert_not_called()
                self.assertTrue(connection.closed)


class PermissionHostRoundtripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="example-permission-")
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "example-project"
        _git_root(self.root)
        self.config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=base / "projects.json",
            state_path=base / "state.db",
            codex_socket_path=base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(ProjectBinding("example-project", -1001234567890),),
            agents=(
                AgentDefinition(
                    "claude",
                    "Claude",
                    "example_claude_bot",
                    "claude",
                    None,
                    False,
                    False,
                    "example-model",
                    "high",
                ),
            ),
        )
        self._registry(self.root)
        self.state = HubState.open(self.config.state_path)
        self.addCleanup(self.state.close)
        self.assertEqual(self.state.schema_version, 38)
        topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=7,
            title="Example",
            execution_root=self.root,
        )
        session = self.state.activate_agent(topic.topic_id, "claude", "example-model", "high")
        job, _ = self.state.enqueue_provider_job(
            idempotency_key="example:1",
            chat_id=topic.chat_id,
            message_id=1,
            topic_id=topic.topic_id,
            agent_id="claude",
            session_id=session.session_id,
            session_generation=session.generation,
            model="example-model",
            effort="high",
            payload_text="Example request",
        )
        leased = self.state.lease_provider_job("claude", "example-worker")
        assert leased is not None and leased.lease_token is not None
        self.token = leased.lease_token
        self.job = job
        self.state.mark_provider_job_executing(job.job_id, self.token)
        binding = ExecutionJournal(self.state).prepare_claude_session(
            job.job_id, self.token, self.root
        )
        self.native_id = binding.session_id
        self.journal = ClaudePermissionJournal(self.state)
        self.launch = self.journal.open_launch(job.job_id, self.token, self.native_id, self.root)
        self.peer = FakeTlive(base / "tlive.sock")
        self.addCleanup(self.peer.close)
        transport = TlivePermissionConfig(
            str(self.peer.path), "42", "42", self.peer.request_key, self.peer.result_key
        )
        client = ProtectedTliveClient(transport)
        capability = client.hello()
        self.server = PermissionServer(
            base / "worker.sock", self.config, self.launch, client, capability
        )
        self.addCleanup(self.server.close)
        self.server.thread.start()
        self.hooks: list[threading.Thread] = []

    def tearDown(self) -> None:
        self.peer.release.set()
        self.server.close()
        for hook in self.hooks:
            hook.join(4)
            self.assertFalse(hook.is_alive(), "native hook did not stop")
        self.journal.close_launch(self.launch)
        self.peer.close()
        self.assertFalse(self.peer.errors, self.peer.errors)

    def _registry(self, root: Path) -> None:
        self.config.registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_roots": [str(self.config.registry_path.parent)],
                    "projects": [
                        {
                            "project_id": "example-project",
                            "display_name": "Example",
                            "topic_name": "Example",
                            "root": str(root),
                        }
                    ],
                }
            )
        )

    def _event(self, **changes: object) -> dict[str, object]:
        event: dict[str, object] = {
            "hook_event_name": "PermissionRequest",
            "session_id": self.native_id,
            "cwd": str(self.root),
            "tool_name": "Write",
            "tool_input": {"file_path": "example.txt", "content": "SENSITIVE_EXAMPLE_INPUT"},
        }
        event.update(changes)
        return event

    def _hook(
        self, event: dict[str, object] | None = None
    ) -> tuple[threading.Thread, dict[str, str]]:
        result: dict[str, str] = {}

        def invoke() -> None:
            output = io.StringIO()
            run_hook(
                io.BytesIO(json.dumps(event or self._event()).encode()),
                output,
                {"HUB_CLAUDE_PERMISSION_SOCKET": str(self.server.path)},
            )
            result["behavior"] = json.loads(output.getvalue())["hookSpecificOutput"]["decision"][
                "behavior"
            ]

        thread = threading.Thread(target=invoke, daemon=True)
        self.hooks.append(thread)
        thread.start()
        return thread, result

    def _await_request(self) -> ProtectedPayload:
        self.assertTrue(self.peer.received.wait(3), "protected request not received")
        return ProtectedPayload.parse(str(self.peer.requests[-1]["payload"]))

    def _finish(self, hook: threading.Thread, result: dict[str, str], expected: str) -> None:
        self.peer.release.set()
        hook.join(4)
        self.assertFalse(hook.is_alive(), "native hook did not return")
        self.assertEqual(result.get("behavior"), expected)

    def _statuses(self) -> list[str]:
        return [
            row[0]
            for row in self.state._connection.execute(
                "SELECT status FROM claude_permission_requests ORDER BY rowid"
            )
        ]

    def test_human_allow_commits_once_before_native_reply(self) -> None:
        hook, result = self._hook()
        payload = self._await_request()
        self.assertEqual(self._statuses(), ["pending"])
        self._finish(hook, result, "allow")
        self.assertEqual(self._statuses(), ["allow"])
        self.assertEqual(payload.launch_epoch, self.launch.epoch)
        self.assertEqual(payload.session_id, self.native_id)
        self.assertEqual(payload.lease_id, self.token)

    def test_namespace_client_preserves_peer_gate_and_atomic_allow_deny(self) -> None:
        bwrap = shutil.which("bwrap")
        python = Path("/usr/bin/python3.12")
        runtime = (
            python,
            Path("/usr/lib/python3.12"),
            Path("/usr/lib/x86_64-linux-gnu"),
            Path("/usr/lib64"),
        )
        if bwrap is None or not all(path.exists() for path in runtime):
            namespace_unavailable(self, "system bubblewrap/Python namespace fixture unavailable")
        home = self.config.state_path.parent / "example-session-home"
        home.mkdir(mode=0o700)
        try:
            sandbox = FileToolSandboxConfig(
                bwrap_executable=Path(bwrap),
                project_root=self.root,
                provider_home=home,
                runtime_roots=runtime,
                claude_executable=python,
                python_executable=python,
                hook_code_root=Path("/usr/lib/python3.12"),
                permission_socket=self.server.path,
                private_paths=(self.config.state_path, self.config.registry_path, self.peer.path),
            )
        except FileToolSandboxError:
            namespace_unavailable(self, "system Python runtime is not immutable root-owned code")
        self.peer.release.set()  # Deterministic fictional human transport, no Telegram/model.
        for decision in ("allow", "deny"):
            self.peer.decision = decision
            event = self._event()
            request = (
                json.dumps(
                    {
                        "kind": "claude.permission.request",
                        "version": 1,
                        "nonce": str(uuid4()),
                        "event": event,
                        "eventDigest": event_digest(event),
                    }
                ).encode()
                + b"\n"
            )
            code = (
                "import os,socket,json\n"
                "with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as connection:\n"
                " connection.settimeout(4)\n"
                " connection.connect('/run/hub-permission.sock')\n"
                f" connection.sendall({request!r})\n"
                " data=bytearray()\n"
                " while True:\n"
                "  chunk=connection.recv(4096)\n"
                "  if not chunk: break\n"
                "  data.extend(chunk)\n"
                "print(json.dumps({'uid':os.getuid(),'reply':json.loads(data)}))\n"
            )
            with sandbox.wrap((str(python), "-c", code), {}, self.root) as launch:
                result = subprocess.run(
                    launch.argv,
                    env=launch.environment,
                    cwd="/",
                    close_fds=True,
                    pass_fds=launch.pass_fds,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
            if result.returncode and "Creating new namespace failed" in result.stderr:
                namespace_unavailable(self, "kernel disallows user namespaces")
            self.assertEqual(result.returncode, 0, result.stderr)
            response = json.loads(result.stdout)
            self.assertEqual(response["uid"], os.getuid())
            self.assertEqual(response["reply"]["decision"], decision)
            self.assertEqual(self._statuses()[-1], decision)
        self.assertEqual(self._statuses(), ["allow", "deny"])
        self.assertEqual(len(self.peer.requests), 2)

    def test_human_deny_is_consumed(self) -> None:
        self.peer.decision = "deny"
        hook, result = self._hook()
        self._await_request()
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["deny"])

    def test_bot_actor_cannot_allow(self) -> None:
        self.peer.actor_change = {"isBot": True}
        hook, result = self._hook()
        self._await_request()
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_wrong_owner_cannot_allow(self) -> None:
        self.peer.actor_change = {"userId": "99"}
        hook, result = self._hook()
        self._await_request()
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_wrong_callback_binding_cannot_allow(self) -> None:
        self.peer.actor_change = {"data": "hp:wrong:a"}
        hook, result = self._hook()
        self._await_request()
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_no_actor_cannot_allow(self) -> None:
        self.peer.actor_none = True
        hook, result = self._hook()
        self._await_request()
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_changed_result_epoch_cannot_allow(self) -> None:
        self.peer.result_change = {"epoch": str(uuid4())}
        hook, result = self._hook()
        self._await_request()
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_bad_result_tag_cannot_allow(self) -> None:
        self.peer.result_change = {"tag": "0" * 64}
        hook, result = self._hook()
        self._await_request()
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_changed_result_payload_cannot_allow(self) -> None:
        self.peer.result_change = {"payload": "{}"}
        hook, result = self._hook()
        self._await_request()
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_replayed_native_nonce_cannot_allow_twice(self) -> None:
        event = self._event()
        nonce = str(uuid4())
        wire = {
            "kind": "claude.permission.request",
            "version": 1,
            "nonce": nonce,
            "event": event,
            "eventDigest": event_digest(event),
        }

        def native_request() -> str:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(4)
                conn.connect(str(self.server.path))
                conn.sendall(json.dumps(wire).encode() + b"\n")
                with conn.makefile("rb") as response:
                    try:
                        raw = response.readline(4096)
                    except ConnectionResetError:
                        return "deny"
                    return json.loads(raw)["decision"] if raw else "deny"

        outcome: dict[str, str] = {}
        thread = threading.Thread(
            target=lambda: outcome.setdefault("first", native_request()), daemon=True
        )
        thread.start()
        self._await_request()
        self.peer.release.set()
        thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["first"], "allow")
        self.assertEqual(native_request(), "deny")
        self.assertEqual(self._statuses(), ["allow"])
        self.assertEqual(len(self.peer.requests), 1)

    def test_registry_root_change_during_human_wait_denies(self) -> None:
        hook, result = self._hook()
        self._await_request()
        replacement = self.root.parent / "other-example-project"
        _git_root(replacement)
        self._registry(replacement)
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_session_change_during_human_wait_denies(self) -> None:
        hook, result = self._hook()
        self._await_request()
        self.state._connection.execute(
            "UPDATE agent_sessions SET writer_mode='local' WHERE session_id=?",
            (self.job.session_id,),
        )
        self.state._connection.commit()
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_generation_change_during_human_wait_denies(self) -> None:
        hook, result = self._hook()
        self._await_request()
        self.state._connection.execute(
            "UPDATE agent_sessions SET generation=generation+1 WHERE session_id=?",
            (self.job.session_id,),
        )
        self.state._connection.commit()
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_lease_change_during_human_wait_denies(self) -> None:
        hook, result = self._hook()
        self._await_request()
        self.state._connection.execute(
            "UPDATE provider_jobs SET lease_token=? WHERE job_id=?", (str(uuid4()), self.job.job_id)
        )
        self.state._connection.commit()
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_stop_during_human_wait_denies(self) -> None:
        hook, result = self._hook()
        self._await_request()
        self.state._connection.execute(
            "INSERT INTO provider_stop_requests VALUES (?,?,?,?,?,'pending',0,?,NULL)",
            ("example-stop", self.job.topic_id, self.job.chat_id, 2, "claude", "9999"),
        )
        self.state._connection.commit()
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_expired_request_denies_even_with_valid_receipt(self) -> None:
        hook, result = self._hook()
        self._await_request()
        self.state._connection.execute("UPDATE claude_permission_requests SET expires_at=1")
        self.state._connection.commit()
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_protected_transport_timeout_denies(self) -> None:
        original = ClaudePermissionJournal.prepare

        def short_deadline(
            journal: ClaudePermissionJournal,
            launch: PermissionLaunch,
            nonce: str,
            digest: str,
            tool: str,
            tool_input: object,
        ) -> str:
            return original(journal, launch, nonce, digest, tool, tool_input, lifetime_seconds=1)

        with patch.object(ClaudePermissionJournal, "prepare", short_deadline):
            hook, result = self._hook()
            self._await_request()
            hook.join(4)
        self.assertFalse(hook.is_alive(), "native hook did not time out")
        self.assertEqual(result.get("behavior"), "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_lost_protected_socket_denies(self) -> None:
        self.peer.drop = True
        hook, result = self._hook()
        self._await_request()
        self._finish(hook, result, "deny")
        self.assertEqual(self._statuses(), ["revoked"])

    def test_host_close_denies_inflight_and_close_launch_revokes(self) -> None:
        hook, result = self._hook()
        self._await_request()
        self.server.close()
        hook.join(4)
        self.assertFalse(hook.is_alive())
        self.assertEqual(result.get("behavior"), "deny")
        self.journal.close_launch(self.launch)
        self.assertEqual(self._statuses(), ["revoked"])

    def test_hidden_input_is_not_persisted_or_echoed_on_denial(self) -> None:
        self.peer.actor_change = {"isBot": True}
        hook, result = self._hook()
        self._await_request()
        self._finish(hook, result, "deny")
        rows = self.state._connection.execute(
            "SELECT * FROM claude_permission_launches, claude_permission_requests, task_lifecycle_notices"
        ).fetchall()
        self.assertNotIn("SENSITIVE_EXAMPLE_INPUT", str([tuple(row) for row in rows]))
        self.assertNotIn("example.txt", str([tuple(row) for row in rows]))

    def test_invalid_envelope_does_not_echo_arbitrary_data(self) -> None:
        wire = {
            "kind": "claude.permission.request",
            "version": 1,
            "nonce": "SENSITIVE_EXAMPLE_NONCE",
            "event": self._event(),
            "eventDigest": "SENSITIVE_EXAMPLE_DIGEST",
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(3)
            connection.connect(str(self.server.path))
            connection.sendall(json.dumps(wire).encode() + b"\n")
            with connection.makefile("rb") as response:
                reply = json.loads(response.readline(4096))
        self.assertEqual(reply["decision"], "deny")
        self.assertIsNone(reply["nonce"])
        self.assertIsNone(reply["eventDigest"])
        self.assertEqual(self._statuses(), [])

    def test_parallel_hook_denies_immediately_without_queue_or_second_card(self) -> None:
        first, first_result = self._hook()
        self._await_request()
        second, second_result = self._hook()
        second.join(3)
        self.assertFalse(second.is_alive())
        self.assertEqual(second_result.get("behavior"), "deny")
        self.assertEqual(len(self.peer.requests), 1)
        self._finish(first, first_result, "allow")

    def test_abandoned_native_hook_revokes_human_wait(self) -> None:
        event = self._event()
        wire = {
            "kind": "claude.permission.request",
            "version": 1,
            "nonce": str(uuid4()),
            "event": event,
            "eventDigest": event_digest(event),
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as native:
            native.connect(str(self.server.path))
            native.sendall(json.dumps(wire).encode() + b"\n")
            self._await_request()
        deadline = time.monotonic() + 3
        while self._statuses() == ["pending"] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self._statuses(), ["revoked"])

    def test_untrusted_native_peers_close_before_read_or_database_open(self) -> None:
        from hermes_codex_router import unix_peer

        for peer_result in ((1, os.geteuid() + 1, 0), OSError("unavailable")):
            with self.subTest(peer_result=peer_result):
                options = (
                    {"return_value": peer_result}
                    if isinstance(peer_result, tuple)
                    else {"side_effect": peer_result}
                )
                with (
                    patch.object(unix_peer, "_peer_credentials", **options),
                    patch.object(
                        HubState, "open", side_effect=AssertionError("opened database")
                    ) as db_open,
                    patch.object(
                        PermissionServer, "_read", side_effect=AssertionError("read request")
                    ) as read,
                ):
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as native:
                        native.settimeout(3)
                        native.connect(str(self.server.path))
                        self.assertEqual(native.recv(1), b"")
                    db_open.assert_not_called()
                    read.assert_not_called()
                self.assertEqual(self.peer.requests, [])
                self.assertEqual(self._statuses(), [])
        self.server.close()
        self.assertFalse(self.server.thread.is_alive())


if __name__ == "__main__":
    unittest.main()
