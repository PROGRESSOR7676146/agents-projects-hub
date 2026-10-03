import io
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from hermes_codex_router.claude_permission_hook import run_hook


class HookTests(unittest.TestCase):
    def test_malformed_and_oversized_stdin_deny(self):
        for raw in (b"{", b"{} {}", b"x" * 65537):
            with self.subTest(size=len(raw)):
                output = io.StringIO()
                self.assertEqual(run_hook(io.BytesIO(raw), output, {}), 0)
                decision = json.loads(output.getvalue())["hookSpecificOutput"]["decision"]
                self.assertEqual(decision["behavior"], "deny")

    def test_missing_socket_denies(self):
        event = {
            "hook_event_name": "PermissionRequest",
            "session_id": str(uuid4()),
            "cwd": "/tmp/example",
            "tool_name": "Read",
            "tool_input": {"file_path": "a"},
        }
        output = io.StringIO()
        run_hook(io.BytesIO(json.dumps(event).encode()), output, {})
        decision = json.loads(output.getvalue())["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")

    def test_agent_child_denies_without_calling_broker(self):
        event = {
            "hook_event_name": "PermissionRequest",
            "session_id": str(uuid4()),
            "cwd": "/tmp/example",
            "tool_name": "Read",
            "tool_input": {},
            "agent_id": "child",
        }
        output = io.StringIO()
        run_hook(
            io.BytesIO(json.dumps(event).encode()),
            output,
            {"HUB_CLAUDE_PERMISSION_SOCKET": "/tmp/no"},
        )
        decision = json.loads(output.getvalue())["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")

    def test_bound_worker_response_allows(self):
        event = {
            "hook_event_name": "PermissionRequest",
            "session_id": str(uuid4()),
            "cwd": "/tmp/example",
            "tool_name": "Read",
            "tool_input": {"file_path": "a"},
        }

        class FakeSocket:
            extra = False
            mismatch = False

            def __init__(self, *_args):
                self.reply = b""

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def settimeout(self, _value):
                pass

            def connect(self, _path):
                pass

            def sendall(self, data):
                request = json.loads(data)
                self.reply = (
                    json.dumps(
                        {
                            "kind": "claude.permission.result",
                            "version": 1,
                            "nonce": request["nonce"],
                            "eventDigest": ("0" * 64 if self.mismatch else request["eventDigest"]),
                            "decision": "allow",
                        }
                    ).encode()
                    + b"\n"
                )
                if self.extra:
                    self.reply += b"{}\n"

            def recv(self, _size):
                result, self.reply = self.reply, b""
                return result

        from hermes_codex_router import claude_permission_hook

        output = io.StringIO()
        with patch.object(claude_permission_hook.socket, "socket", FakeSocket):
            run_hook(
                io.BytesIO(json.dumps(event).encode()),
                output,
                {"HUB_CLAUDE_PERMISSION_SOCKET": "/tmp/worker.sock"},
            )
        decision = json.loads(output.getvalue())["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "allow")
        FakeSocket.extra = True
        output = io.StringIO()
        with patch.object(claude_permission_hook.socket, "socket", FakeSocket):
            run_hook(
                io.BytesIO(json.dumps(event).encode()),
                output,
                {"HUB_CLAUDE_PERMISSION_SOCKET": "/tmp/worker.sock"},
            )
        decision = json.loads(output.getvalue())["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")
        FakeSocket.extra = False
        FakeSocket.mismatch = True
        output = io.StringIO()
        with patch.object(claude_permission_hook.socket, "socket", FakeSocket):
            run_hook(
                io.BytesIO(json.dumps(event).encode()),
                output,
                {"HUB_CLAUDE_PERMISSION_SOCKET": "/tmp/worker.sock"},
            )
        decision = json.loads(output.getvalue())["hookSpecificOutput"]["decision"]
        self.assertEqual(decision["behavior"], "deny")


if __name__ == "__main__":
    unittest.main()
