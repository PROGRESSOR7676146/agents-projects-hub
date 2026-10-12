"""Optional direct-native attribution, never a live provider probe."""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import aiohttp

from tests.codex_native_notification_origin import (
    NativePeer,
    NotificationOriginError,
    attribute_notifications,
    pinned_listener,
)
from tests.codex_native_profile_fixture import NativeProfileFixture


class NativeNotificationOriginTests(unittest.TestCase):
    def test_two_direct_connections_preserve_terminal_turn_and_attribute_frames(self):
        executable = os.environ.get("HUB_NATIVE_CODEX_FIXTURE_EXECUTABLE")
        if not executable:
            if os.environ.get("HUB_REQUIRE_NATIVE_CODEX_PROFILE_TESTS") == "1":
                self.fail("required offline native notification fixture unavailable")
            self.skipTest("explicit offline native notification fixture unavailable")
        assert executable is not None
        with NativeProfileFixture(Path(executable), notification_listener=True) as fixture:
            result = asyncio.run(attribute_notifications(fixture))
        self.assertIs(result["first_exact_completed"], True)
        self.assertEqual(result["passive_responses_requests"], 1)
        self.assertEqual(result["responses_requests"], 2)
        self.assertIs(result["subscribed_control_completed"], True)
        self.assertIs(result["old_subscription_survives_thread_switch"], True)
        self.assertIs(result["observers_healthy"], True)
        self.assertEqual(result["history_turns"], 1)
        self.assertGreater(
            sum(
                row["count"]
                for row in result["first"]
                if row["thread"] == "thread-A" and row["turn"] == "turn-A"
            ),
            1024,
        )


class NativeListenerContainmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="example-listener-pin-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.project = self.base / "example-project"
        self.project.mkdir()
        self.daemon = self.base / "example-daemon"
        self.daemon.mkdir(mode=0o700)
        self.leaf = "a" * 64
        self.listener = self.project / "example-native.sock"
        self.listener.symlink_to(f"/tmp/codex-daemon-{os.getuid()}/{self.leaf}")

    def test_pin_keeps_original_socket_after_leaf_replacement_and_closes_fd(self):
        source = socket.socket(socket.AF_UNIX)
        self.addCleanup(source.close)
        directory = os.open(self.daemon, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            source.bind(f"/proc/self/fd/{directory}/{self.leaf}")
        finally:
            os.close(directory)
        with pinned_listener(self.project, self.daemon) as path:
            inode = path.stat().st_ino
            (self.daemon / self.leaf).unlink()
            (self.daemon / self.leaf).symlink_to(self.base / "example-outside")
            self.assertEqual(path.stat().st_ino, inode)
        self.assertFalse(path.exists())

    def test_rejects_arbitrary_symlink_destination(self):
        self.listener.unlink()
        self.listener.symlink_to(self.base / "example-outside.sock")
        with self.assertRaises(NotificationOriginError):
            with pinned_listener(self.project, self.daemon):
                self.fail("outside endpoint accepted")

    def test_rejects_regular_file_or_symlink_at_valid_leaf(self):
        destination = self.daemon / self.leaf
        destination.write_text("fictional regular file")
        with self.assertRaises(NotificationOriginError):
            with pinned_listener(self.project, self.daemon):
                self.fail("regular file accepted")
        destination.unlink()
        destination.symlink_to(self.base / "example-outside.sock")
        with self.assertRaises(NotificationOriginError):
            with pinned_listener(self.project, self.daemon):
                self.fail("linked endpoint accepted")


class ScriptedWebSocket:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.sent = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        payload = await self.frames.get()
        if payload is None:
            raise StopAsyncIteration
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, json=lambda: payload)

    async def send_json(self, message):
        await self.sent.put(message)


class NativeObserverEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.websocket = ScriptedWebSocket()
        self.observer = NativePeer(cast(aiohttp.ClientWebSocketResponse, self.websocket))

    async def asyncTearDown(self):
        self.observer.closing = True
        self.websocket.frames.put_nowait(None)
        await asyncio.wait_for(self.observer.reader, 1)

    async def test_unexpected_clean_eof_cannot_support_zero_notification_evidence(self):
        self.websocket.frames.put_nowait(None)
        await asyncio.wait_for(self.observer.reader, 1)
        with self.assertRaises(NotificationOriginError):
            self.observer.assert_healthy()

    async def test_initialize_response_stamps_phase_before_following_notification(self):
        initialize = asyncio.create_task(self.observer.initialize())
        request = await asyncio.wait_for(self.websocket.sent.get(), 1)
        self.websocket.frames.put_nowait({"id": request["id"], "result": {}})
        self.websocket.frames.put_nowait(
            {
                "method": "thread/status/changed",
                "params": {"threadId": "example-thread", "status": {"type": "idle"}},
            }
        )
        await asyncio.wait_for(initialize, 1)
        self.assertEqual(
            self.observer.summary("example-thread", "example-turn"),
            [
                {
                    "phase": "initialized",
                    "kind": "notification:thread/status/changed",
                    "thread": "thread-A",
                    "turn": "unknown",
                    "count": 1,
                }
            ],
        )
        self.observer.assert_healthy()


if __name__ == "__main__":
    unittest.main()
