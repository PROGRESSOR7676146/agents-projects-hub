"""Structural attribution from direct native sockets; no raw payload report."""

from __future__ import annotations

import asyncio
import os
import re
import stat
from collections import Counter
from contextlib import ExitStack, asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

import aiohttp

from tests.codex_native_profile_fixture import PROFILE_ID, NativeProfileFixture


class NotificationOriginError(RuntimeError):
    pass


@contextmanager
def pinned_listener(project: Path, directory: Path):
    """Pin only the socket in fresh fixture storage, never a host daemon endpoint."""
    with ExitStack() as cleanup:
        project_fd = os.open(project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        cleanup.callback(os.close, project_fd)
        target = os.readlink("example-native.sock", dir_fd=project_fd)
        match = re.fullmatch(rf"/tmp/codex-daemon-{os.getuid()}/([0-9a-f]{{64}})", target)
        if match is None:
            raise NotificationOriginError("native_listener_target_invalid")
        directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        cleanup.callback(os.close, directory_fd)
        descriptor = os.open(match[1], os.O_PATH | os.O_NOFOLLOW, dir_fd=directory_fd)
        cleanup.callback(os.close, descriptor)
        if not stat.S_ISSOCK(os.fstat(descriptor).st_mode):
            raise NotificationOriginError("native_listener_type_invalid")
        yield Path(f"/proc/self/fd/{descriptor}")


class NativePeer:
    def __init__(self, websocket: aiohttp.ClientWebSocketResponse) -> None:
        self.websocket = websocket
        self.phase = "connected"
        self.counts: Counter[tuple[str, str, str, str]] = Counter()
        self.pending: dict[int, asyncio.Future[dict]] = {}
        self.initialize_id: int | None = None
        self.next_id = 0
        self.sequence = 0
        self.completed: set[tuple[str, str]] = set()
        self.condition = asyncio.Condition()
        self.error = False
        self.closing = False
        self.reader = asyncio.create_task(self.read())

    async def read(self) -> None:
        try:
            async for frame in self.websocket:
                if frame.type != aiohttp.WSMsgType.TEXT:
                    raise NotificationOriginError("native_frame_type_invalid")
                message = frame.json()
                if not isinstance(message, dict):
                    raise NotificationOriginError("native_frame_shape_invalid")
                self.sequence += 1
                if self.sequence > 10_000:
                    raise NotificationOriginError("native_frame_count_bound")
                method = message.get("method")
                if method is None and "id" in message:
                    future = self.pending.pop(message["id"], None)
                    if future is None or future.done():
                        raise NotificationOriginError("native_response_identity_invalid")
                    if "error" in message or not isinstance(message.get("result"), dict):
                        future.set_exception(NotificationOriginError("native_rpc_rejected"))
                    else:
                        if message["id"] == self.initialize_id:
                            # Stamp at the actual wire boundary, before resolving the RPC.
                            self.phase = "initialized"
                        future.set_result(message["result"])
                    continue
                if not isinstance(method, str) or len(method) > 128:
                    raise NotificationOriginError("native_notification_method_invalid")
                params = message.get("params", {})
                if not isinstance(params, dict):
                    raise NotificationOriginError("native_notification_params_invalid")
                thread = params.get("threadId") or params.get("conversationId") or "unknown"
                turn = params.get("turnId") or "unknown"
                if method == "turn/completed":
                    terminal = params.get("turn", {})
                    turn = terminal.get("id", "unknown")
                    if terminal.get("status") == "completed":
                        self.completed.add((thread, turn))
                classification = "request" if "id" in message else "notification"
                label = method
                if method.startswith("codex/event/"):
                    event = params.get("msg", {})
                    label += ":" + str(event.get("type", "unknown"))[:64]
                    turn = params.get("id", turn)
                self.counts[(self.phase, classification + ":" + label, thread, turn)] += 1
                if "id" in message:
                    await self.respond_to_request(message)
                async with self.condition:
                    self.condition.notify_all()
        except (
            ValueError,
            TypeError,
            AttributeError,
            NotificationOriginError,
            aiohttp.ClientError,
        ):
            self.error = True
        finally:
            if not self.closing:
                self.error = True
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(NotificationOriginError("native_reader_stopped"))
            async with self.condition:
                self.condition.notify_all()

    async def respond_to_request(self, message: dict) -> None:
        await self.websocket.send_json(
            {
                "id": message["id"],
                "error": {"code": -32601, "message": "offline attribution fixture denies requests"},
            }
        )

    async def rpc(self, method: str, params: dict) -> dict:
        self.assert_healthy()
        self.next_id += 1
        identifier = self.next_id
        if method == "initialize":
            self.initialize_id = identifier
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = future
        try:
            await self.websocket.send_json({"id": identifier, "method": method, "params": params})
            return await asyncio.wait_for(future, 15)
        finally:
            self.pending.pop(identifier, None)

    async def initialize(self) -> None:
        self.phase = "initialize_pending"
        await self.rpc(
            "initialize",
            {
                "clientInfo": {"name": "example-notification-origin", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.websocket.send_json({"method": "initialized", "params": {}})

    def assert_healthy(self) -> None:
        if self.error or self.reader.done():
            raise NotificationOriginError("native_observer_not_healthy")

    def notifications(self, thread: str, turn: str) -> int:
        return sum(
            value
            for (_, kind, owner, native_turn), value in self.counts.items()
            if kind.startswith("notification:") and owner == thread and native_turn == turn
        )

    def deltas(self, thread: str, turn: str) -> int:
        return sum(
            value
            for (_, kind, owner, native_turn), value in self.counts.items()
            if kind == "notification:item/agentMessage/delta"
            and owner == thread
            and native_turn == turn
        )

    async def wait_until(self, predicate) -> None:
        async with self.condition:
            await asyncio.wait_for(self.condition.wait_for(lambda: predicate() or self.error), 15)
        if self.error:
            raise NotificationOriginError("native_reader_failed")

    def summary(self, thread: str, turn: str) -> list[dict[str, Any]]:
        return [
            {
                "phase": phase,
                "kind": kind,
                "thread": "thread-A" if owner == thread else "unknown",
                "turn": "turn-A" if native_turn == turn else "unknown",
                "count": value,
            }
            for (phase, kind, owner, native_turn), value in sorted(self.counts.items())
        ]


@asynccontextmanager
async def peer(fixture: NativeProfileFixture):
    with pinned_listener(fixture.project, fixture.listener_directory) as socket:
        connector = aiohttp.UnixConnector(path=str(socket))
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.ws_connect("http://localhost/", max_msg_size=4_000_000) as websocket:
                value = NativePeer(websocket)
                try:
                    yield value
                finally:
                    value.closing = True
                    await websocket.close()
                    await asyncio.gather(value.reader, return_exceptions=True)


async def attribute_notifications(fixture: NativeProfileFixture) -> dict:
    fixture.prepare_turn_case(kind="notifications")
    async with peer(fixture) as first:
        await first.initialize()
        params = {
            "cwd": str(fixture.project),
            "model": "example-offline",
            "modelProvider": "example-offline",
            "permissions": PROFILE_ID,
            "approvalPolicy": "never",
        }
        thread = (await first.rpc("thread/start", params))["thread"]["id"]
        turn = (
            await first.rpc(
                "turn/start",
                {
                    **params,
                    "threadId": thread,
                    "input": [
                        {"type": "text", "text": "Example deterministic notification fixture."}
                    ],
                },
            )
        )["turn"]["id"]
        await first.wait_until(lambda: first.deltas(thread, turn) > 0)
        async with peer(fixture) as second:
            fixture._send({"fixture_burst": True})
            await second.initialize()
            await first.wait_until(lambda: first.notifications(thread, turn) > 1024)
            second.phase = "passive_list_pending"
            await second.rpc("thread/list", {"limit": 1})
            second.phase = "passive_list_completed"
            fixture._send({"fixture_finish": True})
            await first.wait_until(lambda: (thread, turn) in first.completed)
            second.phase = "passive_read_pending"
            history = await second.rpc("thread/read", {"threadId": thread, "includeTurns": True})
            history_turns = len(history["thread"].get("turns", []))
            turns = history["thread"].get("turns", [])
            if (
                len(turns) != 1
                or turns[0].get("id") != turn
                or turns[0].get("status") != "completed"
            ):
                raise NotificationOriginError("native_history_turn_not_exact_completed")
            first_primary = first.summary(thread, turn)
            unrelated = second.summary(thread, turn)
            passive_requests = fixture.responses_count()
            second.phase = "explicit_resume_pending"
            await second.rpc("thread/resume", {**params, "threadId": thread, "excludeTurns": True})
            second.phase = "new_thread_pending"
            other_thread = (await second.rpc("thread/start", params))["thread"]["id"]
            if other_thread == thread:
                raise NotificationOriginError("native_control_thread_not_distinct")
            second.phase = "new_thread_completed_old_subscription"
            fixture.prepare_turn_case(kind="notifications")
            control_turn = (
                await first.rpc(
                    "turn/start",
                    {
                        **params,
                        "threadId": thread,
                        "input": [{"type": "text", "text": "Example subscribed positive control."}],
                    },
                )
            )["turn"]["id"]
            await first.wait_until(lambda: first.deltas(thread, control_turn) > 0)
            await second.wait_until(lambda: second.deltas(thread, control_turn) > 0)
            fixture._send({"fixture_burst": True})
            fixture._send({"fixture_finish": True})
            await first.wait_until(lambda: (thread, control_turn) in first.completed)
            await second.wait_until(lambda: (thread, control_turn) in second.completed)
            second.phase = "explicit_unsubscribe_pending"
            await second.rpc("thread/unsubscribe", {"threadId": thread})
            first.assert_healthy()
            second.assert_healthy()
            result = {
                "first": first_primary,
                "unrelated_second": unrelated,
                "second_all": second.summary(thread, turn),
                "history_turns": history_turns,
                "first_exact_completed": True,
                "subscribed_control_completed": True,
                "old_subscription_survives_thread_switch": True,
                "passive_responses_requests": passive_requests,
                "responses_requests": fixture.responses_count(),
            }
        async with peer(fixture) as fresh:
            await fresh.initialize()
            fresh.phase = "idle_list_pending"
            await fresh.rpc("thread/list", {"limit": 1})
            result["fresh_idle"] = fresh.summary(thread, turn)
            fresh.assert_healthy()
            result["observers_healthy"] = True
        return result
