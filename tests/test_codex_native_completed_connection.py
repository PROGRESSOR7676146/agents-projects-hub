"""Opt-in native socket retirement; fictional local responses, no real login."""

from __future__ import annotations

import asyncio
import os
import unittest
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from hermes_codex_router.codex_appserver import CodexAppServerClient, UnixWebSocketTransport
from hermes_codex_router.codex_result_lifecycle import retire_completed_connection
from tests.codex_native_notification_origin import peer, pinned_listener
from tests.codex_native_profile_fixture import PROFILE_ID, NativeProfileFixture


class CountedTransport:
    """Count frames before Hub filtering; never retain native text or reasoning."""

    def __init__(self, transport):
        self.transport = transport
        self.threads = Counter()
        self.methods = Counter()
        self.closed = False

    def send(self, message):
        return self.transport.send(message)

    def receive(self, *, timeout=None):
        message = self.transport.receive(timeout=timeout)
        params = message.get("params", {})
        if isinstance(params, dict) and "method" in message:
            thread = params.get("threadId") or params.get("conversationId")
            if isinstance(thread, str):
                self.threads[thread] += 1
                self.methods[(thread, message["method"])] += 1
        return message

    def close(self):
        self.transport.close()
        self.closed = True

    def subscription_frames(self, thread):
        # Native thread status is broadcast to fresh unrelated connections too.
        # Only that known status method is excluded; any unexpected event fails.
        return sum(
            count
            for (owner, method), count in self.methods.items()
            if owner == thread and method != "thread/status/changed"
        )


@asynccontextmanager
async def hub_client(fixture):
    with pinned_listener(fixture.project, fixture.listener_directory) as socket:
        transport = CountedTransport(await asyncio.to_thread(UnixWebSocketTransport, socket))
        client = CodexAppServerClient(
            transport,
            approval_policy="never",
            model_provider="example-offline",
            permission_profile=PROFILE_ID,
            retire_completed_connection=True,
        )
        try:
            await asyncio.to_thread(client.initialize)
            yield client, transport
        finally:
            await asyncio.to_thread(client.close)


async def complete_hub_turn(fixture, client, thread_id, observer=None):
    await asyncio.to_thread(fixture.prepare_turn_case, kind="notifications")
    turn_id = await asyncio.to_thread(
        client.start_turn,
        thread_id=thread_id,
        cwd=fixture.project,
        text="Example deterministic completion.",
        model="example-offline",
        effort="high",
    )
    waiting = asyncio.create_task(asyncio.to_thread(client.wait_for_turn, turn_id))
    try:
        if observer is not None:
            await observer.wait_until(lambda: observer.deltas(thread_id, turn_id) > 0)
        fixture._send({"fixture_burst": True})
        fixture._send({"fixture_finish": True})
        result = await asyncio.wait_for(asyncio.shield(waiting), 20)
        if observer is not None:
            await observer.wait_until(lambda: (thread_id, turn_id) in observer.completed)
        if result.text != "x" * 1201:
            raise AssertionError("fictional native final output mismatch")
        return turn_id
    except BaseException:
        await asyncio.to_thread(client.close)
        await asyncio.gather(waiting, return_exceptions=True)
        raise


def assert_history(history, expected):
    turns = history["thread"]["turns"]
    if [turn["id"] for turn in turns] != expected:
        raise AssertionError("fictional stored turn identity mismatch")
    for turn in turns:
        finals = [
            item["text"]
            for item in turn.get("items", [])
            if item.get("type") == "agentMessage" and item.get("phase") == "final_answer"
        ]
        if turn["status"] != "completed" or finals != ["x" * 1201]:
            shape = [
                (
                    item.get("type"),
                    item.get("phase"),
                    len(item.get("text", "")),
                    item.get("text") == "x" * 1201,
                )
                for item in turn.get("items", [])
            ]
            raise AssertionError(
                f"fictional stored final result mismatch: {turn['status']}, {shape}"
            )


async def retire_native_connections(fixture):
    warnings = []
    params = {
        "cwd": str(fixture.project),
        "model": "example-offline",
        "modelProvider": "example-offline",
        "permissions": PROFILE_ID,
        "approvalPolicy": "never",
    }
    async with peer(fixture) as survivor:
        await survivor.initialize()
        async with hub_client(fixture) as (old, old_wire):
            thread_a = (
                await asyncio.to_thread(
                    old.start_thread,
                    cwd=fixture.project,
                    model="example-offline",
                    project_id="example",
                )
            ).thread_id
            turn_a1 = await complete_hub_turn(fixture, old, thread_a)
            if old_wire.subscription_frames(thread_a) <= 1024:
                raise AssertionError("fictional raw counter lacks subscribed positive control")
            assert_history(
                await survivor.rpc("thread/read", {"threadId": thread_a, "includeTurns": True}),
                [turn_a1],
            )
            await survivor.rpc(
                "thread/resume", {**params, "threadId": thread_a, "excludeTurns": True}
            )
            await asyncio.to_thread(
                retire_completed_connection,
                old,
                thread_id=thread_a,
                turn_id=turn_a1,
                retire=old.close,
                warning=lambda *args: warnings.append(args),
            )
            if not old_wire.closed:
                raise AssertionError("completed fictional connection was not retired")

        # A different connection remains productive while the fresh Hub prepares B.
        await asyncio.to_thread(fixture.prepare_turn_case, kind="notifications")
        turn_a2 = (
            await survivor.rpc(
                "turn/start",
                {
                    **params,
                    "threadId": thread_a,
                    "input": [{"type": "text", "text": "Example surviving connection."}],
                },
            )
        )["turn"]["id"]
        await survivor.wait_until(lambda: survivor.deltas(thread_a, turn_a2) > 0)
        async with hub_client(fixture) as (fresh, fresh_wire):
            fixture._send({"fixture_burst": True})
            await survivor.wait_until(lambda: survivor.deltas(thread_a, turn_a2) > 1024)
            thread_b = (
                await asyncio.to_thread(
                    fresh.start_thread,
                    cwd=fixture.project,
                    model="example-offline",
                    project_id="example",
                )
            ).thread_id
            if thread_b == thread_a:
                raise AssertionError("fictional control thread was not distinct")
            fixture._send({"fixture_finish": True})
            await survivor.wait_until(lambda: (thread_a, turn_a2) in survivor.completed)
            # A same-connection RPC response is the observation barrier, not elapsed sleep.
            await asyncio.to_thread(
                fresh.read_thread_metadata, thread_id=thread_a, cwd=fixture.project
            )
            if fresh_wire.subscription_frames(thread_a) != 0:
                shape = [
                    (method, count)
                    for (owner, method), count in fresh_wire.methods.items()
                    if owner == thread_a
                ]
                raise AssertionError(
                    f"fresh fictional connection inherited old subscription: {shape}"
                )
            turn_b = await complete_hub_turn(fixture, fresh, thread_b)
            assert_history(
                await survivor.rpc("thread/read", {"threadId": thread_b, "includeTurns": True}),
                [turn_b],
            )
            await asyncio.to_thread(
                fresh.read_thread_metadata, thread_id=thread_a, cwd=fixture.project
            )
            if fresh_wire.subscription_frames(thread_a) != 0:
                raise AssertionError("productive fictional connection inherited old subscription")
            await asyncio.to_thread(
                retire_completed_connection,
                fresh,
                thread_id=thread_b,
                turn_id=turn_b,
                retire=fresh.close,
                warning=lambda *args: warnings.append(args),
            )
            if not fresh_wire.closed:
                raise AssertionError("second fictional connection was not retired")

        async with hub_client(fixture) as (resumed, _):
            requests = await asyncio.to_thread(fixture.responses_count)
            selected = await asyncio.to_thread(
                resumed.resume_thread,
                thread_id=thread_a,
                cwd=fixture.project,
                model="example-offline",
            )
            if (
                selected.thread_id != thread_a
                or await asyncio.to_thread(fixture.responses_count) != requests
            ):
                raise AssertionError("fictional exact resume invoked responses or changed identity")
            turn_a3 = await complete_hub_turn(fixture, resumed, thread_a, survivor)
            assert_history(
                await survivor.rpc("thread/read", {"threadId": thread_a, "includeTurns": True}),
                [turn_a1, turn_a2, turn_a3],
            )
            assert_history(
                await survivor.rpc("thread/read", {"threadId": thread_b, "includeTurns": True}),
                [turn_b],
            )
            survivor.assert_healthy()
            if await asyncio.to_thread(fixture.responses_count) != 4 or warnings:
                raise AssertionError("fictional request count or retirement warnings mismatch")


class NativeCompletedConnectionTests(unittest.TestCase):
    def test_retired_socket_preserves_other_peer_and_fresh_exact_native_resume(self):
        executable = os.environ.get("HUB_NATIVE_CODEX_FIXTURE_EXECUTABLE")
        if not executable:
            if os.environ.get("HUB_REQUIRE_NATIVE_CODEX_PROFILE_TESTS") == "1":
                self.fail("required offline native completion fixture unavailable")
            self.skipTest("explicit offline native completion fixture unavailable")
        assert executable is not None
        with NativeProfileFixture(Path(executable), notification_listener=True) as fixture:
            asyncio.run(retire_native_connections(fixture))

    def test_saved_history_requires_exact_completed_final_not_only_terminal_status(self):
        correct = {
            "id": "example-turn",
            "status": "completed",
            "items": [{"type": "agentMessage", "phase": "final_answer", "text": "x" * 1201}],
        }
        assert_history({"thread": {"turns": [correct]}}, ["example-turn"])
        for invalid in (
            {**correct, "id": "other-turn"},
            {**correct, "status": "failed"},
            {**correct, "items": []},
            {**correct, "items": correct["items"] * 2},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AssertionError):
                    assert_history({"thread": {"turns": [invalid]}}, ["example-turn"])
