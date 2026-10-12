"""Opt-in real native turn continues while fictional Telegram ingress is lost."""

from __future__ import annotations

import asyncio
import json
import os
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import cast

from hermes_codex_router.codex_appserver import CodexAppServerClient
from hermes_codex_router.codex_live_control import CodexLiveControl
from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.state import HubState
from tests.codex_native_notification_origin import peer
from tests.codex_native_profile_fixture import NativeProfileFixture
from tests.hub_service_harness import HubHarness
from tests.test_codex_native_completed_connection import hub_client


async def ingress_loss_with_primary_alive(fixture: NativeProfileFixture) -> None:
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
                text="Example offline ingress-loss fixture.",
                model="example-offline",
                effort="high",
            )
            waiting = asyncio.create_task(asyncio.to_thread(primary.wait_for_turn, turn))
            harness = await asyncio.to_thread(HubHarness, fixture.base / "example-hub")
            try:
                document = json.loads(harness.config.registry_path.read_text())
                document["allowed_roots"] = [str(fixture.base)]
                document["projects"][0]["root"] = str(fixture.project)
                harness.config.registry_path.write_text(json.dumps(document))

                def prepare():
                    with closing(
                        HubState.open_existing(
                            harness.config.state_path, codex_permission_profile=None
                        )
                    ) as state:
                        topic = state.observe_topic(
                            project_id="example-project",
                            chat_id=-1001234567890,
                            thread_id=77,
                            title="Example native ingress",
                            execution_root=fixture.project,
                        )
                        session = state.activate_agent(
                            topic.topic_id, "codex", "example-offline", "high"
                        )
                        state.bind_provider_session(session.session_id, thread, None)
                        job, _ = state.enqueue_provider_job(
                            idempotency_key="example-native-ingress",
                            chat_id=topic.chat_id,
                            message_id=1,
                            topic_id=topic.topic_id,
                            agent_id="codex",
                            session_id=session.session_id,
                            session_generation=session.generation,
                            model=session.model,
                            effort=session.effort,
                            payload_text="Example offline ingress-loss fixture.",
                            telegram_ingress_identity="hub",
                        )
                        lease = state.lease_provider_job("codex", "example-native-worker")
                        assert lease is not None and lease.lease_token is not None
                        job = state.mark_provider_job_executing(job.job_id, lease.lease_token)
                        journal = ExecutionJournal(state)
                        journal.record_thread(
                            job.job_id, lease.lease_token, thread, fixture.project
                        )
                        journal.record_turn(job.job_id, lease.lease_token, turn)
                        failed_at = datetime.now(timezone.utc) - timedelta(seconds=31)
                        publisher = state.telegram_ingress.register(
                            "hub",
                            instance_token="example-native-ingress",
                            previous_epoch=0,
                            now=failed_at,
                        )
                        for sequence in (1, 2, 3):
                            state.telegram_ingress.record_poll(
                                publisher, sequence=sequence, succeeded=False, observed_at=failed_at
                            )
                        return job

                job = await asyncio.to_thread(prepare)
                active_deadline = time.monotonic() + 10
                while primary_wire.methods[(thread, "item/agentMessage/delta")] == 0:
                    if waiting.done() or time.monotonic() >= active_deadline:
                        raise AssertionError("fictional primary did not remain active")
                    await asyncio.sleep(0.02)
                history = await observer.rpc(
                    "thread/read", {"threadId": thread, "includeTurns": True}
                )
                turns = history["thread"].get("turns", [])
                if (
                    len(turns) != 1
                    or turns[0]["id"] != turn
                    or turns[0]["status"] != "inProgress"
                    or primary_wire.closed
                ):
                    raise AssertionError(
                        "native work did not continue through fictional ingress loss"
                    )
                async with hub_client(fixture) as (client, wire):
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

                    def control():
                        with closing(
                            HubState.open_existing(
                                harness.config.state_path, codex_permission_profile=None
                            )
                        ) as state:
                            live = CodexLiveControl(
                                config=harness.config,
                                state_factory=lambda: state,
                                client_factory=lambda: cast(CodexAppServerClient, client),
                                job=job,
                                worker_id="example-native-worker",
                                thread_id=thread,
                                turn_id=turn,
                                transport_mode="socket",
                                close_owned_turn_client=primary.close,
                            )
                            if not live._poll_ingress(state):
                                raise AssertionError("native ingress precaution was not serviced")
                            retained = state.codex_controls.read(job.job_id)
                            if (
                                retained is None
                                or retained["interrupt_outcome"] != "matched_ack"
                                or retained["owner_quiesced_at"] is None
                            ):
                                raise AssertionError("ingress ACK lost its durable fence")
                            if (
                                state.codex_ingress_control.read_cause(job.job_id) is None
                                or state.pending_emergency_stop_for_job(job.job_id) is not None
                            ):
                                raise AssertionError(
                                    "ingress cause was lost or owner stop fabricated"
                                )

                    await asyncio.to_thread(control)
                    if calls.count("turn/interrupt") != 1 or any(
                        method in calls
                        for method in ("turn/start", "thread/start", "thread/resume", "turn/steer")
                    ):
                        raise AssertionError("ingress control duplicated productive work")
                deadline = time.monotonic() + 5
                while True:
                    history = await observer.rpc(
                        "thread/read", {"threadId": thread, "includeTurns": True}
                    )
                    turns = history["thread"].get("turns", [])
                    if (
                        len(turns) == 1
                        and turns[0]["id"] == turn
                        and turns[0]["status"] == "interrupted"
                    ):
                        break
                    if time.monotonic() >= deadline:
                        raise AssertionError("exact native interruption not observed independently")
                    await asyncio.sleep(0.02)
                if await asyncio.to_thread(fixture.responses_count) != 1:
                    raise AssertionError("ingress precaution invoked another scripted response")
                observer.assert_healthy()
            finally:
                await asyncio.to_thread(primary.close)
                await asyncio.gather(waiting, return_exceptions=True)
                await asyncio.to_thread(harness.close)


class NativeIngressLossTests(unittest.TestCase):
    def test_native_turn_continues_after_ingress_loss_then_one_exact_precaution(self):
        executable = os.environ.get("HUB_NATIVE_CODEX_FIXTURE_EXECUTABLE")
        if not executable:
            if os.environ.get("HUB_REQUIRE_NATIVE_CODEX_PROFILE_TESTS") == "1":
                self.fail("required offline native ingress fixture unavailable")
            self.skipTest("explicit offline native ingress fixture unavailable")
        assert executable is not None
        with NativeProfileFixture(Path(executable), notification_listener=True) as fixture:
            asyncio.run(ingress_loss_with_primary_alive(fixture))
