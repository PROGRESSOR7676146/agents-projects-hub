"""Opt-in exact native control after losing only the disposable Hub connection."""

from __future__ import annotations

import asyncio
import os
import time
import unittest
from pathlib import Path

from hermes_codex_router.codex_control_recovery import observe_after_control_loss
from tests.codex_native_notification_origin import peer
from tests.codex_native_profile_fixture import NativeProfileFixture
from tests.test_codex_native_completed_connection import hub_client


async def control_after_disconnect(fixture):
    async with peer(fixture) as observer:
        await observer.initialize()
        async with hub_client(fixture) as (primary, primary_wire):
            thread = (
                await asyncio.to_thread(
                    primary.start_thread,
                    cwd=fixture.project,
                    model="example-offline",
                    project_id="example",
                )
            ).thread_id
            await asyncio.to_thread(fixture.prepare_turn_case, kind="notifications")
            turn = await asyncio.to_thread(
                primary.start_turn,
                thread_id=thread,
                cwd=fixture.project,
                text="Example offline control-loss fixture.",
                model="example-offline",
                effort="high",
            )
            waiting = asyncio.create_task(asyncio.to_thread(primary.wait_for_turn, turn))
            try:
                active_deadline = time.monotonic() + 10
                while primary_wire.methods[(thread, "item/agentMessage/delta")] == 0:
                    if waiting.done() or time.monotonic() >= active_deadline:
                        raise AssertionError("fictional primary stream did not become active")
                    await asyncio.sleep(0.02)
                await asyncio.to_thread(primary.close)
                failure = await asyncio.wait_for(
                    asyncio.gather(waiting, return_exceptions=True), 10
                )
                if not isinstance(failure[0], Exception):
                    raise AssertionError("lost fictional connection unexpectedly completed")
                # Closing the owned primary connection did not stop the native
                # task. This is the independent protocol witness for that risk.
                history = await observer.rpc(
                    "thread/read", {"threadId": thread, "includeTurns": True}
                )
                turns = history["thread"].get("turns", [])
                if len(turns) != 1 or turns[0]["id"] != turn or turns[0]["status"] != "inProgress":
                    raise AssertionError("fictional native task did not survive connection loss")
                async with hub_client(fixture) as (control, wire):
                    calls = []
                    send = wire.send

                    def counted_send(message):
                        calls.append(message.get("method"))
                        return send(message)

                    wire.send = counted_send
                    outcome = await asyncio.to_thread(
                        observe_after_control_loss,
                        control,
                        thread_id=thread,
                        turn_id=turn,
                        root=fixture.project,
                        may_interrupt=lambda: True,
                    )
                    if outcome.status not in {"active", "interrupted"}:
                        raise AssertionError("fictional production reread has no exact outcome")
                    # A separate bounded read can observe a terminal transition
                    # after ACK; it never submits another interrupt or new turn.
                    deadline = time.monotonic() + 5
                    while outcome.status == "active" and time.monotonic() < deadline:
                        await asyncio.sleep(0.02)
                        outcome = await asyncio.to_thread(
                            control.read_turn_outcome,
                            thread_id=thread,
                            turn_id=turn,
                            cwd=fixture.project,
                            deadline=deadline,
                        )
                    if outcome.status != "interrupted":
                        raise AssertionError("fictional exact native interruption not proven")
                    if wire.closed:
                        raise AssertionError("fictional fresh control connection was lost")
                    if calls.count("turn/interrupt") != 1 or any(
                        method in calls
                        for method in ("turn/start", "thread/start", "thread/resume", "turn/steer")
                    ):
                        raise AssertionError("fictional control submission duplicated or replayed")
                observer.assert_healthy()
                if await asyncio.to_thread(fixture.responses_count) != 1:
                    raise AssertionError("control recovery invoked another scripted response")
            finally:
                await asyncio.to_thread(primary.close)
                await asyncio.gather(waiting, return_exceptions=True)


class NativeControlLossTests(unittest.TestCase):
    def test_native_turn_survives_primary_close_then_exact_control_interrupts_without_replay(self):
        executable = os.environ.get("HUB_NATIVE_CODEX_FIXTURE_EXECUTABLE")
        if not executable:
            if os.environ.get("HUB_REQUIRE_NATIVE_CODEX_PROFILE_TESTS") == "1":
                self.fail("required offline native control-loss fixture unavailable")
            self.skipTest("explicit offline native control-loss fixture unavailable")
        assert executable is not None
        with NativeProfileFixture(Path(executable), notification_listener=True) as fixture:
            asyncio.run(control_after_disconnect(fixture))
