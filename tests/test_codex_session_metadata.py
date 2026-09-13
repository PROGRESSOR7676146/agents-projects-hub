from __future__ import annotations

import copy
import tempfile
import time
import unittest
from pathlib import Path

from test_codex_appserver import FakeTransport

from hermes_codex_router.codex_appserver import CodexAppServerClient, RpcError


class CodexSessionMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        # Reviewed subset of codex-cli 0.154.0 generated ThreadReadResponse.
        self.metadata = {
            "id": "example-thread",
            "sessionId": "different-session-tree-root",
            "cwd": str(self.root),
            "modelProvider": "openai",
            "ephemeral": False,
            "source": "cli",
            "historyMode": "legacy",
            "status": {"type": "notLoaded"},
            "preview": "private preview must never be returned",
            "turns": [],
        }

    def read(self, metadata: object):
        transport = FakeTransport([{"id": 1, "result": {"thread": metadata}}])
        client = CodexAppServerClient(transport, initialized=True)
        result = client.read_thread_metadata(thread_id="example-thread", cwd=self.root)
        self.assertEqual(
            transport.sent,
            [
                {
                    "method": "thread/read",
                    "id": 1,
                    "params": {"threadId": "example-thread", "includeTurns": False},
                }
            ],
        )
        self.assertTrue(
            all(value is not None and 0 < value <= 10 for value in transport.receive_timeouts)
        )
        return result

    def test_metadata_is_exact_bounded_and_not_a_session_tree_id(self) -> None:
        result = self.read(self.metadata)
        self.assertEqual(result.thread_id, "example-thread")
        self.assertEqual(result.cwd, self.root)
        self.assertEqual(result.model_provider, "openai")
        self.assertNotIn("private preview", repr(result))

    def test_rejects_unsupported_or_mismatched_metadata(self) -> None:
        changes = [
            ("id", "different-thread"),
            ("id", None),
            ("cwd", None),
            ("cwd", "."),
            ("cwd", str(self.root.parent)),
            ("ephemeral", True),
            ("ephemeral", None),
            ("modelProvider", "custom"),
            ("status", {"type": "active", "activeFlags": ["waitingOnApproval"]}),
            ("status", {"type": "systemError"}),
            ("status", {}),
            ("status", {"type": "idle", "activeFlags": ["waitingOnApproval"]}),
            ("source", "unknown"),
            ("source", {"subAgent": "review"}),
            ("historyMode", "unsupported"),
            ("turns", [{"secret": "private"}]),
        ]
        for key, value in changes:
            with self.subTest(key=key, value=value):
                metadata = copy.deepcopy(self.metadata)
                metadata[key] = value
                with self.assertRaises(RpcError):
                    self.read(metadata)

    def test_expired_deadline_cannot_send_request(self) -> None:
        transport = FakeTransport([])
        client = CodexAppServerClient(transport, initialized=True)
        with self.assertRaises(RpcError):
            client.read_thread_metadata(
                thread_id="example-thread", cwd=self.root, deadline=time.monotonic() - 1
            )
        self.assertEqual(transport.sent, [])

    def test_initialize_and_read_share_one_deadline_despite_notifications(self) -> None:
        transport = FakeTransport(
            [
                {"id": 1, "result": {}},
                {"method": "thread/status/changed", "params": {}},
                {"id": 2, "result": {"thread": self.metadata}},
            ]
        )
        client = CodexAppServerClient(transport)
        deadline = time.monotonic() + 5
        client.initialize(deadline=deadline)
        client.read_thread_metadata(thread_id="example-thread", cwd=self.root, deadline=deadline)
        self.assertTrue(
            all(value is not None and 0 < value <= 5 for value in transport.receive_timeouts)
        )
        self.assertEqual(
            sorted(value for value in transport.receive_timeouts if value is not None)[::-1],
            transport.receive_timeouts,
        )
