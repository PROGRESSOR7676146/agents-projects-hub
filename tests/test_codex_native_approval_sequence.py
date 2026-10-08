"""Real native approvals, synthetic decline only, fixed local Responses budgets."""

from __future__ import annotations

import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from pathlib import Path

import aiohttp

from hermes_codex_router.codex_activity import normalize_codex_activity
from tests.codex_native_notification_origin import (
    NativePeer,
    NotificationOriginError,
    pinned_listener,
)
from tests.codex_native_profile_fixture import PROFILE_ID, NativeProfileFixture
from tests.test_codex_native_completed_connection import complete_hub_turn, hub_client


class SyntheticDenyPeer(NativePeer):
    def __init__(self, websocket, thread, limit):
        self.thread = thread
        self.turn = None
        self.limit = limit
        self.accepted = asyncio.Event()
        self.declined = set()
        super().__init__(websocket)

    async def respond_to_request(self, message):
        await asyncio.wait_for(self.accepted.wait(), 5)
        event = normalize_codex_activity(
            message, expected_thread_id=self.thread, expected_turn_id=self.turn
        )
        if (
            event is None
            or event.kind != "approval_requested"
            or event.category != "command"
            or len(self.declined) >= self.limit
        ):
            raise NotificationOriginError("synthetic_deny_scope_invalid")
        key = (type(event.request_id), event.request_id)
        if key in self.declined:
            raise NotificationOriginError("synthetic_deny_duplicate")
        self.declined.add(key)
        await self.websocket.send_json({"id": message["id"], "result": {"decision": "decline"}})


@asynccontextmanager
async def deny_companion(fixture, thread, limit):
    with pinned_listener(fixture.project, fixture.listener_directory) as socket:
        connector = aiohttp.UnixConnector(path=str(socket))
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.ws_connect("http://localhost/", max_msg_size=4_000_000) as websocket:
                companion = SyntheticDenyPeer(websocket, thread, limit)
                try:
                    await companion.initialize()
                    await companion.rpc(
                        "thread/resume",
                        {
                            "threadId": thread,
                            "cwd": str(fixture.project),
                            "model": "example-offline",
                            "modelProvider": "example-offline",
                            "permissions": PROFILE_ID,
                            "approvalPolicy": "on-request",
                            "excludeTurns": True,
                        },
                    )
                    yield companion
                finally:
                    companion.closing = True
                    await websocket.close()
                    await asyncio.gather(companion.reader, return_exceptions=True)


async def sequential_native_approvals(fixture, *, compatibility):
    count = 2 if compatibility else 129
    kind = "approval_compatibility" if compatibility else "approval_sequence"
    async with hub_client(fixture, approval_policy="on-request") as (primary, wire):
        thread = (
            await asyncio.to_thread(
                primary.start_thread,
                cwd=fixture.project,
                model="example-offline",
                project_id="example",
            )
        ).thread_id
        # Persist one completed disposable thread before subscribing a second
        # native client. An empty native thread is not yet resumable.
        await complete_hub_turn(fixture, primary, thread)
        async with deny_companion(fixture, thread, count) as companion:
            await asyncio.to_thread(fixture.prepare_turn_case, kind=kind)
            events = []
            pending = set()
            peak = 0
            resolved = 0

            def observe(event):
                nonlocal peak, resolved
                if event.kind not in ("approval_requested", "approval_resolved"):
                    return
                key = (type(event.request_id), event.request_id)
                if event.kind == "approval_requested":
                    if key in pending:
                        raise AssertionError("fictional approval reopened")
                    pending.add(key)
                    peak = max(peak, len(pending))
                else:
                    pending.remove(key)
                    resolved += 1
                    fixture._send({"fixture_approval_resolved": resolved})
                events.append(event)

            primary.on_activity = observe
            turn = await asyncio.to_thread(
                primary.start_turn,
                thread_id=thread,
                cwd=fixture.project,
                text="Example sequential synthetic denials; never allow a request.",
                model="example-offline",
                effort="high",
            )
            companion.turn = turn
            companion.accepted.set()
            waiting = asyncio.create_task(asyncio.to_thread(primary.wait_for_turn, turn))
            try:
                result = await asyncio.wait_for(asyncio.shield(waiting), 300)
                if result.text != "Example approval sequence complete.":
                    raise AssertionError("fictional exact approval final missing")
                if (
                    resolved != count
                    or len(events) != count * 2
                    or pending
                    or peak != 1
                    or len(companion.declined) != count
                    or any(event.thread_id != thread or event.turn_id != turn for event in events)
                ):
                    raise AssertionError("fictional native approvals not exact and sequential")
                history = await companion.rpc(
                    "thread/read", {"threadId": thread, "includeTurns": True}
                )
                exact = [
                    item for item in history["thread"].get("turns", []) if item.get("id") == turn
                ]
                if len(exact) != 1 or exact[0]["status"] != "completed":
                    raise AssertionError("fictional native completion not retained")
                if await asyncio.to_thread(fixture.responses_count) != count + 2:
                    raise AssertionError("fictional response budget or replay mismatch")
                companion.assert_healthy()
                if wire.closed:
                    raise AssertionError("fictional owning approval stream retired")
            finally:
                await asyncio.to_thread(primary.close)
                await asyncio.gather(waiting, return_exceptions=True)


class NativeApprovalSequenceTests(unittest.TestCase):
    def run_case(self, compatibility):
        executable = os.environ.get("HUB_NATIVE_CODEX_FIXTURE_EXECUTABLE")
        if not executable:
            if os.environ.get("HUB_REQUIRE_NATIVE_CODEX_PROFILE_TESTS") == "1":
                self.fail("required offline native approval fixture unavailable")
            self.skipTest("explicit offline native approval fixture unavailable")
        assert executable is not None
        with NativeProfileFixture(Path(executable), notification_listener=True) as fixture:
            asyncio.run(sequential_native_approvals(fixture, compatibility=compatibility))

    def test_two_sequential_native_approvals_compatibility(self):
        self.run_case(True)

    def test_more_than_pending_bound_sequential_native_approvals_keep_exact_final(self):
        self.run_case(False)
