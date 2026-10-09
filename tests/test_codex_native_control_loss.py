"""Opt-in exact native control after losing only the disposable Hub connection."""

from __future__ import annotations

import asyncio
import os
import time
import unittest
from contextlib import closing
from pathlib import Path

from hermes_codex_router.codex_control_recovery import observe_after_control_loss
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.state import HubState
from tests.codex_native_notification_origin import peer
from tests.codex_native_profile_fixture import NativeProfileFixture
from tests.test_codex_native_completed_connection import hub_client


def accepted_journal(path, root, thread, turn):
    with closing(HubState.open(path, codex_permission_profile=None)) as state:
        topic = state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=77,
            title="Example native fixture",
            execution_root=root,
        )
        session = state.activate_agent(topic.topic_id, "codex", "example-offline", "high")
        state.bind_provider_session(session.session_id, thread, None)
        job, _ = state.enqueue_provider_job(
            idempotency_key="example-native",
            chat_id=topic.chat_id,
            message_id=1,
            topic_id=topic.topic_id,
            agent_id="codex",
            session_id=session.session_id,
            session_generation=session.generation,
            model=session.model,
            effort=session.effort,
            payload_text="Example offline control-loss fixture.",
        )
        lease = state.lease_provider_job("codex", "example-worker")
        assert lease is not None and lease.lease_token is not None
        state.mark_provider_job_executing(job.job_id, lease.lease_token)
        journal = ExecutionJournal(state)
        journal.record_thread(job.job_id, lease.lease_token, thread, root)
        journal.record_turn(job.job_id, lease.lease_token, turn)
        return job.job_id, lease.lease_token


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
            path = fixture.base / "example-control.db"
            job_id, invocation_token = await asyncio.to_thread(
                accepted_journal, path, fixture.project, thread, turn
            )

            def begin(proof, deadline):
                with closing(HubState.open_existing(path, codex_permission_profile=None)) as state:
                    return state.codex_controls.begin_interrupt(
                        job_id=job_id,
                        source="protective",
                        proof=proof,
                        validated_root=str(fixture.project),
                        invocation_token=invocation_token,
                        send_deadline=deadline,
                    )

            def finish(owner, outcome):
                with closing(HubState.open_existing(path, codex_permission_profile=None)) as state:
                    state.codex_controls.finish_interrupt(
                        job_id, owner, outcome=outcome, send_path_quiesced=outcome != "unknown"
                    )

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
                    send_before = wire.send_before

                    def counted_send(message):
                        calls.append(message.get("method"))
                        return send(message)

                    def counted_send_before(message, *, deadline):
                        calls.append(message.get("method"))
                        return send_before(message, deadline=deadline)

                    wire.send = counted_send
                    wire.send_before = counted_send_before
                    outcome = await asyncio.to_thread(
                        observe_after_control_loss,
                        control,
                        thread_id=thread,
                        turn_id=turn,
                        root=fixture.project,
                        begin_interrupt=begin,
                        finish_interrupt=finish,
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
                    with closing(
                        HubState.open_existing(path, codex_permission_profile=None)
                    ) as state:
                        retained = state.codex_controls.read(job_id)
                        if (
                            retained is None
                            or retained["interrupt_outcome"] != "matched_ack"
                            or retained["owner_quiesced_at"] is None
                        ):
                            raise AssertionError(
                                "native matched response lost its durable sender fence"
                            )
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
