from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TypedDict
from unittest.mock import patch

from hermes_codex_router import state as state_module
from hermes_codex_router.state import HubState, StateError


class BurstJobCommon(TypedDict):
    chat_id: int
    topic_id: int
    agent_id: str
    session_id: str
    session_generation: int
    model: str
    effort: str
    context_watermark: int | None
    quiet_ms: int
    max_ms: int


class ProviderJobQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "private" / "hub.db"
        self.state = HubState.open(self.path)
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=77,
            title="Example topic",
        )
        self.codex = self.state.activate_agent(self.topic.topic_id, "codex", "gpt-example", "high")
        self.context_turn_id = self.state.record_visible_turn(
            self.topic.topic_id,
            agent_id="opencode",
            provider="opencode",
            model="gpt-example",
            user_excerpt="prior visible request",
            response_excerpt="prior visible response",
        )

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def enqueue(
        self,
        message_id: int,
        *,
        agent_id: str = "codex",
        session_id: str | None = None,
        generation: int | None = None,
        model: str = "gpt-example",
        effort: str = "high",
        context_watermark: int | None = None,
        provider_session_id: str | None = None,
    ):
        return self.state.enqueue_provider_job(
            idempotency_key=f"telegram:-1001234567890:{message_id}",
            chat_id=self.topic.chat_id,
            message_id=message_id,
            topic_id=self.topic.topic_id,
            agent_id=agent_id,
            session_id=session_id or self.codex.session_id,
            session_generation=generation or self.codex.generation,
            provider_session_id=provider_session_id,
            model=model,
            effort=effort,
            payload_text=f"bounded request {message_id}",
            context_watermark=(
                self.context_turn_id if context_watermark is None else context_watermark
            ),
        )

    def test_open_enables_wal_foreign_keys_and_busy_timeout(self) -> None:
        connection = self.state._connection
        self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertGreaterEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 5000)

    def test_enqueue_is_idempotent_and_snapshots_bounded_fields(self) -> None:
        first, created = self.enqueue(501)
        duplicate, duplicate_created = self.enqueue(501)

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first.job_id, duplicate.job_id)
        self.assertEqual(first.topic_sequence, 1)
        self.assertEqual(first.status, "queued")
        self.assertEqual(first.session_generation, self.codex.generation)
        self.assertEqual(first.context_watermark, self.context_turn_id)
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM observed_messages WHERE chat_id = ? AND message_id = ?",
                (self.topic.chat_id, 501),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            [
                tuple(row)
                for row in self.state._connection.execute(
                    "SELECT part_index FROM provider_job_inputs WHERE job_id = ?",
                    (first.job_id,),
                ).fetchall()
            ],
            [(1,)],
        )

    def test_burst_inputs_append_to_one_delayed_job_and_remain_idempotent(self) -> None:
        common: BurstJobCommon = {
            "chat_id": self.topic.chat_id,
            "topic_id": self.topic.topic_id,
            "agent_id": "codex",
            "session_id": self.codex.session_id,
            "session_generation": self.codex.generation,
            "model": "gpt-example",
            "effort": "high",
            "context_watermark": self.context_turn_id,
            "quiet_ms": 1500,
            "max_ms": 8000,
        }
        first, first_accepted = self.state.enqueue_or_append_provider_job(
            **common,
            idempotency_key="telegram:burst:601",
            message_id=601,
            payload_text="context wrapper\nCURRENT USER MESSAGE:\nfirst",
            appended_user_text="first",
        )
        second, second_accepted = self.state.enqueue_or_append_provider_job(
            **common,
            idempotency_key="telegram:burst:602",
            message_id=602,
            payload_text="duplicated context must not be used",
            appended_user_text="second",
        )
        duplicate, duplicate_accepted = self.state.enqueue_or_append_provider_job(
            **common,
            idempotency_key="telegram:burst:602",
            message_id=602,
            payload_text="ignored duplicate",
            appended_user_text="second",
        )

        self.assertTrue(first_accepted)
        self.assertTrue(second_accepted)
        self.assertFalse(duplicate_accepted)
        self.assertEqual(first.job_id, second.job_id)
        self.assertEqual(second.job_id, duplicate.job_id)
        self.assertIn("CURRENT USER MESSAGE:\nfirst", second.payload_text)
        self.assertIn("FOLLOW-UP USER MESSAGE (same Telegram burst):\nsecond", second.payload_text)
        self.assertNotIn("duplicated context", second.payload_text)
        self.assertIsNotNone(second.next_attempt_at)
        self.assertEqual(
            [
                tuple(row)
                for row in self.state._connection.execute(
                    """SELECT message_id, part_index, input_text
                       FROM provider_job_inputs WHERE job_id = ? ORDER BY part_index""",
                    (first.job_id,),
                ).fetchall()
            ],
            [(601, 1, "context wrapper\nCURRENT USER MESSAGE:\nfirst"), (602, 2, "second")],
        )
        self.assertIsNone(self.state.lease_provider_job("codex", "worker-now"))
        assert second.next_attempt_at is not None
        ready = datetime.fromisoformat(second.next_attempt_at) + timedelta(milliseconds=1)
        leased = self.state.lease_provider_job("codex", "worker-later", now=ready)
        self.assertIsNotNone(leased)
        assert leased is not None
        self.assertEqual(leased.job_id, first.job_id)

    def test_burst_does_not_append_across_an_intervening_topic_job(self) -> None:
        first, _ = self.state.enqueue_or_append_provider_job(
            idempotency_key="telegram:burst:630",
            chat_id=self.topic.chat_id,
            message_id=630,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=self.codex.session_id,
            session_generation=self.codex.generation,
            model=self.codex.model,
            effort=self.codex.effort,
            payload_text="first",
            appended_user_text="first",
            quiet_ms=1500,
            max_ms=8000,
        )
        other = self.state.ensure_satellite(
            self.topic.topic_id, "opencode", "provider-selected", "high"
        )
        self.state.enqueue_provider_job(
            idempotency_key="telegram:burst:631",
            chat_id=self.topic.chat_id,
            message_id=631,
            topic_id=self.topic.topic_id,
            agent_id="opencode",
            session_id=other.session_id,
            session_generation=other.generation,
            model=other.model,
            effort=other.effort,
            payload_text="intervening",
        )
        last, _ = self.state.enqueue_or_append_provider_job(
            idempotency_key="telegram:burst:632",
            chat_id=self.topic.chat_id,
            message_id=632,
            topic_id=self.topic.topic_id,
            agent_id="codex",
            session_id=self.codex.session_id,
            session_generation=self.codex.generation,
            model=self.codex.model,
            effort=self.codex.effort,
            payload_text="last",
            appended_user_text="last",
            quiet_ms=1500,
            max_ms=8000,
        )
        self.assertNotEqual(first.job_id, last.job_id)
        self.assertEqual(last.topic_sequence, 3)

    def test_emergency_stop_cancels_queue_and_remains_pending_for_active_job(self) -> None:
        active, _ = self.enqueue(610)
        queued, _ = self.enqueue(611)
        leased = self.state.lease_provider_job("codex", "worker")
        assert leased is not None and leased.lease_token is not None
        executing = self.state.mark_provider_job_executing(leased.job_id, leased.lease_token)

        request_id, cancelled, pending = self.state.request_emergency_stop(
            topic_id=self.topic.topic_id,
            chat_id=self.topic.chat_id,
            message_id=612,
            target_agent_id="codex",
        )

        self.assertEqual(executing.job_id, active.job_id)
        self.assertEqual(cancelled, 1)
        self.assertTrue(pending)
        self.assertEqual(self.state.get_provider_job(queued.job_id).status, "cancelled")
        self.assertEqual(
            self.state.pending_emergency_stop(self.topic.topic_id, "codex"), request_id
        )
        self.state.cancel_active_provider_job(executing.job_id, leased.lease_token)
        self.state.complete_emergency_stop(request_id)
        self.assertEqual(self.state.get_provider_job(active.job_id).status, "cancelled")
        self.assertIsNone(self.state.pending_emergency_stop(self.topic.topic_id, "codex"))

    def satellite_job(self, message_id: int):
        satellite = self.state.ensure_satellite(
            self.topic.topic_id, "opencode", "provider-selected", "high"
        )
        return self.enqueue(
            message_id,
            agent_id="opencode",
            session_id=satellite.session_id,
            generation=satellite.generation,
            model=satellite.model,
        )

    def test_emergency_stop_cancels_every_provider_queue_in_the_topic(self) -> None:
        codex_job, _ = self.enqueue(640)
        satellite_job, _ = self.satellite_job(641)

        request_id, cancelled, pending = self.state.request_emergency_stop(
            topic_id=self.topic.topic_id,
            chat_id=self.topic.chat_id,
            message_id=642,
            target_agent_id="codex",
        )

        self.assertEqual((cancelled, pending), (2, False))
        for job in (codex_job, satellite_job):
            self.assertEqual(self.state.get_provider_job(job.job_id).status, "cancelled")
        self.assertIsNone(self.state.pending_emergency_stop(self.topic.topic_id, "codex"))
        self.assertTrue(
            self.state.enqueue_emergency_stop_notice(request_id, "Активной работы нет.")
        )

    def test_emergency_stop_targets_the_running_turn_not_the_active_agent(self) -> None:
        running, _ = self.satellite_job(643)
        leased = self.state.lease_provider_job("opencode", "opencode-worker")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(leased.job_id, leased.lease_token)
        queued, _ = self.enqueue(644)

        request_id, cancelled, pending = self.state.request_emergency_stop(
            topic_id=self.topic.topic_id,
            chat_id=self.topic.chat_id,
            message_id=645,
            target_agent_id="codex",
        )

        self.assertEqual((cancelled, pending), (1, True))
        self.assertEqual(self.state.get_provider_job(queued.job_id).status, "cancelled")
        self.assertEqual(
            self.state.pending_emergency_stop(self.topic.topic_id, "opencode"), request_id
        )
        self.assertIsNone(self.state.pending_emergency_stop(self.topic.topic_id, "codex"))
        self.assertTrue(
            self.state.enqueue_emergency_stop_notice(request_id, "Останавливаю активную работу.")
        )
        notice = self.state.get_telegram_outbox_for_job(running.job_id)
        self.assertEqual(notice.sender_agent_id, "hub")

    def stop(self, message_id: int) -> tuple[str, int, bool]:
        return self.state.request_emergency_stop(
            topic_id=self.topic.topic_id,
            chat_id=self.topic.chat_id,
            message_id=message_id,
            target_agent_id="codex",
        )

    def test_emergency_stop_leaves_held_work_for_the_owner_decision(self) -> None:
        cause, _ = self.enqueue(650)
        held_queued, _ = self.satellite_job(651)
        held_retry, _ = self.enqueue(652)
        free, _ = self.enqueue(653)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET status = 'retry_wait' WHERE job_id = ?",
                (held_retry.job_id,),
            )
            for job in (held_queued, held_retry):
                self.state._connection.execute(
                    """INSERT INTO provider_job_holds (job_id, cause_job_id, held_at)
                       VALUES (?, ?, '2026-09-27T00:00:00+00:00')""",
                    (job.job_id, cause.job_id),
                )

        _, cancelled, _ = self.stop(654)

        self.assertEqual(cancelled, 2)
        self.assertEqual(self.state.get_provider_job(held_queued.job_id).status, "queued")
        self.assertEqual(self.state.get_provider_job(held_retry.job_id).status, "retry_wait")
        for job in (cause, free):
            self.assertEqual(self.state.get_provider_job(job.job_id).status, "cancelled")

    def test_duplicate_stop_never_attaches_its_notice_to_later_work(self) -> None:
        satellite, _ = self.satellite_job(655)
        request_id, cancelled, pending = self.stop(656)
        self.assertEqual((cancelled, pending), (1, False))
        self.assertTrue(self.state.enqueue_emergency_stop_notice(request_id, "Stopped."))
        notice = self.state.lease_telegram_outbox("hub", "sender")
        assert notice is not None and notice.lease_token is not None
        self.state.mark_telegram_outbox_delivered(
            notice.outbox_id, notice.lease_token, telegram_message_id=657
        )
        later, _ = self.enqueue(658)
        leased = self.state.lease_provider_job("codex", "worker")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(leased.job_id, leased.lease_token)

        duplicate_id, _, _ = self.stop(656)
        self.assertEqual(duplicate_id, request_id)
        self.assertTrue(self.state.enqueue_emergency_stop_notice(duplicate_id, "Stopped."))

        with self.assertRaises(StateError):
            self.state.get_telegram_outbox_for_job(later.job_id)
        self.assertEqual(
            self.state.get_telegram_outbox_for_job(satellite.job_id).sender_agent_id, "hub"
        )

    def test_stop_recorded_after_lease_cancels_the_job_before_it_starts(self) -> None:
        job, _ = self.satellite_job(659)
        leased = self.state.lease_provider_job("opencode", "opencode-worker")
        assert leased is not None and leased.lease_token is not None
        request_id, _, pending = self.stop(660)
        self.assertTrue(pending)

        started = self.state.mark_provider_job_executing(
            leased.job_id, leased.lease_token, honor_stop=True
        )

        self.assertEqual((started.job_id, started.status), (job.job_id, "cancelled"))
        self.assertIsNone(self.state.pending_emergency_stop(self.topic.topic_id, "opencode"))
        self.assertIsNotNone(request_id)

    def test_an_older_unfinished_stop_does_not_cancel_later_work(self) -> None:
        first, _ = self.enqueue(661)
        leased = self.state.lease_provider_job("codex", "worker")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(leased.job_id, leased.lease_token)
        _, _, pending = self.stop(662)
        self.assertTrue(pending)
        self.state.cancel_active_provider_job(first.job_id, leased.lease_token)
        # The stop stays pending, as after a worker crash before completing it.
        later, _ = self.enqueue(663)
        next_lease = self.state.lease_provider_job("codex", "worker")
        assert next_lease is not None and next_lease.lease_token is not None

        started = self.state.mark_provider_job_executing(
            next_lease.job_id, next_lease.lease_token, honor_stop=True
        )

        self.assertEqual((started.job_id, started.status), (later.job_id, "executing"))
        self.assertIsNone(self.state.pending_emergency_stop_for_job(later.job_id))

    def test_stop_cannot_commit_between_the_start_check_and_the_transition(self) -> None:
        job, _ = self.satellite_job(664)
        leased = self.state.lease_provider_job("opencode", "opencode-worker")
        assert leased is not None and leased.lease_token is not None
        recorded = threading.Event()

        def record_stop() -> None:
            peer = HubState.open(self.path)
            try:
                peer.request_emergency_stop(
                    topic_id=self.topic.topic_id,
                    chat_id=self.topic.chat_id,
                    message_id=665,
                    target_agent_id="codex",
                )
            finally:
                peer.close()
                recorded.set()

        writer = threading.Thread(target=record_stop)
        committed_inside: list[bool] = []

        def after_stop_check(statement: str) -> None:
            # The transition statement starts only after the stop check read.
            if "SET status = 'executing'" in statement and not committed_inside:
                writer.start()
                committed_inside.append(recorded.wait(0.5))

        self.state._connection.set_trace_callback(after_stop_check)
        try:
            started = self.state.mark_provider_job_executing(
                leased.job_id, leased.lease_token, honor_stop=True
            )
        finally:
            self.state._connection.set_trace_callback(None)
        writer.join(10)

        self.assertEqual(committed_inside, [False], "a stop committed inside the start")
        self.assertEqual(started.status, "executing")
        self.assertIsNotNone(self.state.pending_emergency_stop_for_job(job.job_id))

    def test_cancelling_stopped_work_completes_every_covering_stop_at_once(self) -> None:
        job, _ = self.enqueue(666)
        leased = self.state.lease_provider_job("codex", "worker")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(leased.job_id, leased.lease_token)
        first, _, pending = self.stop(667)
        second, _, pending_again = self.stop(668)
        self.assertTrue(pending and pending_again)
        self.assertNotEqual(first, second)
        self.assertEqual(self.state.pending_emergency_stop_for_job(job.job_id), first)

        with self.assertRaises(StateError):
            self.state.cancel_active_provider_job(job.job_id, "wrong-token", complete_stops=True)
        self.assertEqual(self.state.pending_emergency_stop_for_job(job.job_id), first)
        self.state.cancel_active_provider_job(job.job_id, leased.lease_token, complete_stops=True)

        self.assertEqual(self.state.get_provider_job(job.job_id).status, "cancelled")
        self.assertIsNone(self.state.pending_emergency_stop_for_job(job.job_id))
        self.assertIsNone(self.state.pending_emergency_stop(self.topic.topic_id, "codex"))

    def test_stop_covers_exactly_the_work_committed_before_it(self) -> None:
        done = threading.Event()
        started: list[str] = []

        def start_job_elsewhere() -> None:
            peer = HubState.open(self.path)
            try:
                job, _ = peer.enqueue_provider_job(
                    idempotency_key="telegram:-1001234567890:671",
                    chat_id=self.topic.chat_id,
                    message_id=671,
                    topic_id=self.topic.topic_id,
                    agent_id="codex",
                    session_id=self.codex.session_id,
                    session_generation=self.codex.generation,
                    model="gpt-example",
                    effort="high",
                    payload_text="bounded request 671",
                    context_watermark=self.context_turn_id,
                )
                leased = peer.lease_provider_job("codex", "worker")
                assert leased is not None and leased.lease_token is not None
                peer.mark_provider_job_executing(leased.job_id, leased.lease_token)
                started.append(job.job_id)
            finally:
                peer.close()
                done.set()

        writer = threading.Thread(target=start_job_elsewhere)
        stop_thread = threading.get_ident()
        committed_first: list[bool] = []
        real_now = state_module._now

        def clock_read_during_stop() -> str:
            # Another writer tries to start work right after the stop's clock read.
            value = real_now()
            if threading.get_ident() == stop_thread and not committed_first:
                writer.start()
                committed_first.append(done.wait(0.5))
            return value

        with patch.object(state_module, "_now", side_effect=clock_read_during_stop):
            request_id, _, pending = self.stop(672)
        writer.join(10)

        covering = self.state.pending_emergency_stop_for_job(started[0])
        if committed_first == [True]:
            # The job existed when the stop was recorded, so the stop covers it.
            self.assertEqual((pending, covering), (True, request_id))
        else:
            self.assertEqual((pending, covering), (False, None))
        self.assertEqual(committed_first, [False], "the stop read its clock before the lock")

    def running_parent_and_follow_up(self, first: int, second: int):
        """A running parent turn (with its lease token) and a queued follow-up."""
        self.enqueue(first)
        parent = self.state.lease_provider_job("codex", "worker")
        assert parent is not None and parent.lease_token is not None
        self.state.mark_provider_job_executing(parent.job_id, parent.lease_token)
        follow_up, _ = self.enqueue(second)
        return parent, follow_up

    def test_stop_between_follow_up_lease_and_start_cancels_only_the_follow_up(self) -> None:
        parent, follow_up = self.running_parent_and_follow_up(673, 674)
        leased = self.state.lease_steer_followup(parent.job_id, "worker-steer")
        assert leased is not None and leased.lease_token is not None
        request_id, _, pending = self.stop(675)
        self.assertTrue(pending)

        started = self.state.start_steer_followup(
            follow_up.job_id, leased.lease_token, parent_job_id=parent.job_id
        )

        self.assertEqual(started.status, "cancelled")
        # The stop still has to interrupt the running parent.
        self.assertEqual(self.state.pending_emergency_stop_for_job(parent.job_id), request_id)

    def test_follow_up_the_stop_does_not_cover_returns_to_the_queue(self) -> None:
        parent, follow_up = self.running_parent_and_follow_up(676, 677)
        leased = self.state.lease_steer_followup(parent.job_id, "worker-steer")
        assert leased is not None and leased.lease_token is not None
        request_id, _, _ = self.stop(678)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET created_at = '9999-12-31T00:00:00+00:00' WHERE job_id = ?",
                (follow_up.job_id,),
            )

        started = self.state.start_steer_followup(
            follow_up.job_id, leased.lease_token, parent_job_id=parent.job_id
        )

        self.assertEqual((started.status, started.lease_token), ("queued", None))
        self.assertEqual(self.state.pending_emergency_stop_for_job(parent.job_id), request_id)

    def test_no_follow_up_joins_a_turn_a_stop_is_ending(self) -> None:
        parent, _ = self.enqueue(679)
        leased = self.state.lease_provider_job("codex", "worker")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(leased.job_id, leased.lease_token)
        self.stop(680)
        later, _ = self.enqueue(681)

        self.assertIsNone(self.state.lease_steer_followup(parent.job_id, "worker-steer"))
        self.assertEqual(self.state.get_provider_job(later.job_id).status, "queued")
        self.assertIsNone(self.state.pending_emergency_stop_for_job(later.job_id))

    def test_held_follow_up_never_joins_a_running_turn(self) -> None:
        parent, follow_up = self.running_parent_and_follow_up(682, 683)
        with self.state._connection:
            self.state._connection.execute(
                """INSERT INTO provider_job_holds (job_id, cause_job_id, held_at)
                   VALUES (?, ?, '2026-09-27T00:00:00+00:00')""",
                (follow_up.job_id, parent.job_id),
            )

        self.assertIsNone(self.state.lease_steer_followup(parent.job_id, "worker-steer"))
        self.assertEqual(self.state.get_provider_job(follow_up.job_id).status, "queued")

    def test_follow_up_rejected_during_a_stop_keeps_the_stop_until_it_is_stopped(self) -> None:
        parent, follow_up = self.running_parent_and_follow_up(686, 687)
        assert parent.lease_token is not None
        leased = self.state.lease_steer_followup(parent.job_id, "worker-steer")
        assert leased is not None and leased.lease_token is not None
        started = self.state.start_steer_followup(
            follow_up.job_id, leased.lease_token, parent_job_id=parent.job_id
        )
        self.assertEqual(started.status, "executing")
        # The owner's stop lands while the steering call is in flight, and the
        # app-server then rejects the call, which returns the follow-up.
        request_id, _, pending = self.stop(688)
        self.assertTrue(pending)
        self.state.reject_unaccepted_steer(follow_up.job_id, leased.lease_token)
        self.state.cancel_active_provider_job(
            parent.job_id, parent.lease_token, complete_stops=True
        )

        self.assertEqual(self.state.pending_emergency_stop_for_job(follow_up.job_id), request_id)
        again = self.state.lease_provider_job("codex", "worker")
        assert again is not None and again.lease_token is not None
        self.assertEqual(again.job_id, follow_up.job_id)
        stopped = self.state.mark_provider_job_executing(
            again.job_id, again.lease_token, honor_stop=True
        )
        self.assertEqual(stopped.status, "cancelled")
        self.assertIsNone(self.state.pending_emergency_stop(self.topic.topic_id, "codex"))

    def test_stop_stays_pending_while_a_leased_follow_up_can_still_start(self) -> None:
        parent, follow_up = self.running_parent_and_follow_up(689, 690)
        assert parent.lease_token is not None
        leased = self.state.lease_steer_followup(parent.job_id, "worker-steer")
        assert leased is not None and leased.lease_token is not None
        request_id, _, _ = self.stop(691)
        self.state.cancel_active_provider_job(
            parent.job_id, parent.lease_token, complete_stops=True
        )
        self.assertEqual(self.state.pending_emergency_stop_for_job(follow_up.job_id), request_id)

        started = self.state.start_steer_followup(
            follow_up.job_id, leased.lease_token, parent_job_id=parent.job_id
        )

        self.assertEqual(started.status, "cancelled")
        self.assertIsNone(self.state.pending_emergency_stop(self.topic.topic_id, "codex"))

    def test_follow_up_of_a_turn_that_ended_returns_to_the_queue(self) -> None:
        parent, follow_up = self.running_parent_and_follow_up(692, 693)
        assert parent.lease_token is not None
        leased = self.state.lease_steer_followup(parent.job_id, "worker-steer")
        assert leased is not None and leased.lease_token is not None
        self.state.commit_provider_result(
            parent.job_id,
            parent.lease_token,
            visible_response="done",
            sender_agent_id="codex",
            telegram_html="done",
        )

        started = self.state.start_steer_followup(
            follow_up.job_id, leased.lease_token, parent_job_id=parent.job_id
        )

        self.assertEqual((started.status, started.lease_token), ("queued", None))

    def test_a_later_stop_completes_an_earlier_one_it_leaves_without_work(self) -> None:
        parent, follow_up = self.running_parent_and_follow_up(694, 695)
        assert parent.lease_token is not None
        leased = self.state.lease_steer_followup(parent.job_id, "worker-steer")
        assert leased is not None and leased.lease_token is not None
        self.state.start_steer_followup(
            follow_up.job_id, leased.lease_token, parent_job_id=parent.job_id
        )
        first, _, pending = self.stop(696)
        self.assertTrue(pending)
        self.state.reject_unaccepted_steer(follow_up.job_id, leased.lease_token)
        self.state.cancel_active_provider_job(
            parent.job_id, parent.lease_token, complete_stops=True
        )
        self.assertEqual(self.state.pending_emergency_stop_for_job(follow_up.job_id), first)

        # A second stop cancels the returned follow-up in the queue directly.
        _, cancelled, pending_again = self.stop(697)

        self.assertEqual((cancelled, pending_again), (1, False))
        self.assertEqual(self.state.get_provider_job(follow_up.job_id).status, "cancelled")
        pending_stops = self.state._connection.execute(
            "SELECT COUNT(*) FROM provider_stop_requests WHERE status = 'pending'"
        ).fetchone()[0]
        self.assertEqual(pending_stops, 0)

    def test_follow_up_without_a_stop_starts_normally(self) -> None:
        parent, follow_up = self.running_parent_and_follow_up(684, 685)
        leased = self.state.lease_steer_followup(parent.job_id, "worker-steer")
        assert leased is not None and leased.lease_token is not None

        started = self.state.start_steer_followup(
            follow_up.job_id, leased.lease_token, parent_job_id=parent.job_id
        )

        self.assertEqual(started.status, "executing")

    def test_emergency_stop_notice_is_durable_idempotent_and_keeps_cancelled_job(self) -> None:
        active, _ = self.enqueue(613)
        leased = self.state.lease_provider_job("codex", "worker")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(leased.job_id, leased.lease_token)
        request_id, _, _ = self.state.request_emergency_stop(
            topic_id=self.topic.topic_id,
            chat_id=self.topic.chat_id,
            message_id=614,
            target_agent_id="codex",
        )

        self.assertTrue(
            self.state.enqueue_emergency_stop_notice(request_id, "Останавливаю активную работу.")
        )
        self.assertTrue(
            self.state.enqueue_emergency_stop_notice(request_id, "Останавливаю активную работу.")
        )
        notice = self.state.get_telegram_outbox_for_job(active.job_id)
        self.assertEqual((notice.sender_agent_id, notice.status), ("hub", "pending"))

        self.state.cancel_active_provider_job(active.job_id, leased.lease_token)
        self.state.complete_emergency_stop(request_id)
        sending = self.state.lease_telegram_outbox("hub", "sender")
        assert sending is not None and sending.lease_token is not None
        self.state.mark_telegram_outbox_delivered(
            sending.outbox_id, sending.lease_token, telegram_message_id=615
        )
        self.assertEqual(self.state.get_provider_job(active.job_id).status, "cancelled")

    def test_compatible_fifo_successor_can_be_absorbed_into_active_turn(self) -> None:
        parent, _ = self.enqueue(620)
        child, _ = self.enqueue(621)
        parent_lease = self.state.lease_provider_job("codex", "worker")
        assert parent_lease is not None and parent_lease.lease_token is not None
        self.state.mark_provider_job_executing(parent.job_id, parent_lease.lease_token)

        followup = self.state.lease_steer_followup(parent.job_id, "steer-worker")
        assert followup is not None and followup.lease_token is not None
        self.assertEqual(followup.job_id, child.job_id)
        self.state.mark_provider_job_executing(followup.job_id, followup.lease_token)
        self.state.complete_steered_job(
            followup.job_id,
            followup.lease_token,
            parent_job_id=parent.job_id,
            provider_turn_id="turn-example",
        )

        self.assertEqual(self.state.get_provider_job(child.job_id).status, "completed")
        self.assertEqual(
            self.state._connection.execute(
                "SELECT parent_job_id FROM provider_job_absorptions WHERE child_job_id = ?",
                (child.job_id,),
            ).fetchone()[0],
            parent.job_id,
        )

    def test_enqueue_rejects_oversized_or_mismatched_snapshot(self) -> None:
        with self.assertRaisesRegex(StateError, "payload"):
            self.state.enqueue_provider_job(
                idempotency_key="telegram:oversized",
                chat_id=self.topic.chat_id,
                message_id=502,
                topic_id=self.topic.topic_id,
                agent_id="codex",
                session_id=self.codex.session_id,
                session_generation=self.codex.generation,
                model="gpt-example",
                effort="high",
                payload_text="x" * 20001,
            )
        with self.assertRaisesRegex(StateError, "session snapshot"):
            self.enqueue(503, generation=self.codex.generation + 1)
        trusted = self.state.bind_provider_session(
            self.codex.session_id, "trusted-provider-session", None
        )
        derived, _ = self.enqueue(504)
        self.assertEqual(derived.provider_session_id, trusted.provider_session_id)
        with self.assertRaisesRegex(StateError, "provider session"):
            self.enqueue(505, provider_session_id="foreign-provider-session")
        with self.assertRaisesRegex(StateError, "model"):
            self.enqueue(506, model="different-model")
        self.state.cancel_provider_job(derived.job_id)
        self.state.set_writer_mode(self.codex.session_id, "local")
        with self.assertRaisesRegex(StateError, "writer"):
            self.enqueue(507)

    def test_enqueue_rejects_context_watermark_from_another_topic_or_future_turn(self) -> None:
        other_topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=self.topic.chat_id,
            thread_id=78,
            title="Other topic",
        )
        other_turn = self.state.record_visible_turn(
            other_topic.topic_id,
            agent_id="opencode",
            provider="opencode",
            model="gpt-example",
            user_excerpt="other request",
            response_excerpt="other response",
        )
        with self.assertRaisesRegex(StateError, "context watermark"):
            self.enqueue(506, context_watermark=other_turn)
        with self.assertRaisesRegex(StateError, "context watermark"):
            self.enqueue(507, context_watermark=other_turn + 100)

    def test_strict_fifo_blocks_other_provider_in_same_topic(self) -> None:
        first, _ = self.enqueue(510)
        satellite = self.state.ensure_satellite(
            self.topic.topic_id, "opencode", "provider-selected", "high"
        )
        second, _ = self.enqueue(
            511,
            agent_id="opencode",
            session_id=satellite.session_id,
            generation=satellite.generation,
            model=satellite.model,
            effort=satellite.effort,
        )

        self.assertIsNone(self.state.lease_provider_job("opencode", "worker-open"))
        leased = self.state.lease_provider_job("codex", "worker-codex")
        self.assertIsNotNone(leased)
        assert leased is not None
        self.assertEqual(leased.job_id, first.job_id)
        self.assertTrue(leased.lease_token)
        self.state.fail_provider_job(
            first.job_id, leased.lease_token or "", error_class="permanent", error_code="bad"
        )
        next_job = self.state.lease_provider_job("opencode", "worker-open")
        self.assertIsNotNone(next_job)
        assert next_job is not None
        self.assertEqual(next_job.job_id, second.job_id)

    def test_indeterminate_provider_blocks_root_until_operator_resolution(self) -> None:
        first, _ = self.enqueue(512)
        satellite = self.state.ensure_satellite(
            self.topic.topic_id, "opencode", "provider-selected", "high"
        )
        second, _ = self.enqueue(
            513,
            agent_id="opencode",
            session_id=satellite.session_id,
            generation=satellite.generation,
            model=satellite.model,
            effort=satellite.effort,
        )

        leased = self.state.lease_provider_job("codex", "worker-codex")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(first.job_id, leased.lease_token)
        self.state.mark_provider_job_indeterminate(
            first.job_id, leased.lease_token, error_code="provider_failure"
        )

        self.assertIsNone(self.state.lease_provider_job("opencode", "worker-open"))
        self.state.resolve_indeterminate_job(first.job_id, "acknowledged")
        next_job = self.state.lease_provider_job("opencode", "worker-open")
        self.assertIsNotNone(next_job)
        assert next_job is not None
        self.assertEqual(next_job.job_id, second.job_id)

    def test_lease_token_guards_execution_heartbeat_and_result(self) -> None:
        queued, _ = self.enqueue(520)
        leased = self.state.lease_provider_job("codex", "worker-codex")
        assert leased is not None and leased.lease_token is not None
        with self.assertRaisesRegex(StateError, "lease"):
            self.state.mark_provider_job_executing(queued.job_id, "wrong-token")

        executing = self.state.mark_provider_job_executing(queued.job_id, leased.lease_token)
        self.assertEqual(executing.status, "executing")
        self.assertEqual(executing.attempt_count, 1)
        renewed = self.state.heartbeat_provider_job(
            queued.job_id, leased.lease_token, lease_seconds=180
        )
        self.assertGreater(renewed.lease_expires_at or "", leased.lease_expires_at or "")
        with self.assertRaisesRegex(StateError, "lease"):
            self.state.commit_provider_result(
                queued.job_id,
                "wrong-token",
                visible_response="done",
                sender_agent_id="codex",
                telegram_html="done",
            )

    def test_stale_leased_requeues_but_stale_executing_is_indeterminate(self) -> None:
        first, _ = self.enqueue(530)
        second_topic = self.state.observe_topic(
            project_id="second-example-project",
            chat_id=-1001234567890,
            thread_id=78,
            title="Second topic",
        )
        second_session = self.state.activate_agent(
            second_topic.topic_id, "opencode", "gpt-example", "high"
        )
        second, _ = self.state.enqueue_provider_job(
            idempotency_key="telegram:-1001234567890:531",
            chat_id=second_topic.chat_id,
            message_id=531,
            topic_id=second_topic.topic_id,
            agent_id="opencode",
            session_id=second_session.session_id,
            session_generation=second_session.generation,
            model="gpt-example",
            effort="high",
            payload_text="second bounded request",
        )
        past = datetime(2026, 1, 1, tzinfo=timezone.utc)
        leased_first = self.state.lease_provider_job(
            "codex", "worker-one", lease_seconds=1, max_parallel_roots=2, now=past
        )
        leased_second = self.state.lease_provider_job(
            "opencode", "worker-two", lease_seconds=1, max_parallel_roots=2, now=past
        )
        assert leased_first is not None and leased_first.lease_token is not None
        assert leased_second is not None and leased_second.lease_token is not None
        self.state.mark_provider_job_executing(second.job_id, leased_second.lease_token, now=past)

        recovered = self.state.recover_stale_provider_jobs(now=past + timedelta(seconds=2))

        self.assertEqual(recovered.requeued_job_ids, (first.job_id,))
        self.assertEqual(recovered.indeterminate_job_ids, (second.job_id,))
        self.assertEqual(self.state.get_provider_job(first.job_id).status, "queued")
        self.assertEqual(self.state.get_provider_job(second.job_id).status, "indeterminate")

    def test_result_and_outbox_commit_atomically_then_delivery_completes_job(self) -> None:
        queued, _ = self.enqueue(540)
        leased = self.state.lease_provider_job("codex", "worker-codex")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(queued.job_id, leased.lease_token)

        result = self.state.commit_provider_result(
            queued.job_id,
            leased.lease_token,
            visible_response="bounded visible result",
            sender_agent_id="codex",
            telegram_html="<b>bounded result</b>",
            provider_session_id="provider-session-after",
            actual_model="gpt-example",
            safe_metadata_json='{"quota":"ok"}',
            acknowledge_context=True,
        )

        self.assertEqual(result.job_id, queued.job_id)
        self.assertEqual(self.state.get_provider_job(queued.job_id).status, "result_ready")
        excerpt = self.state._connection.execute(
            """SELECT user_excerpt, response_excerpt, model, provider
               FROM external_turn_excerpts WHERE topic_id = ? ORDER BY turn_id DESC LIMIT 1""",
            (self.topic.topic_id,),
        ).fetchone()
        assert excerpt is not None
        self.assertEqual(excerpt["user_excerpt"], queued.payload_text)
        self.assertEqual(excerpt["response_excerpt"], "bounded visible result")
        self.assertEqual(excerpt["model"], "gpt-example")
        self.assertEqual(excerpt["provider"], "codex")
        outbox = self.state.get_telegram_outbox_for_job(queued.job_id)
        self.assertEqual(outbox.status, "pending")
        sending = self.state.lease_telegram_outbox("codex", "sender-one")
        assert sending is not None and sending.lease_token is not None
        self.state.mark_telegram_outbox_delivered(
            sending.outbox_id, sending.lease_token, telegram_message_id=9001
        )
        self.assertEqual(self.state.get_provider_job(queued.job_id).status, "completed")
        self.assertEqual(
            self.state.get_telegram_outbox_for_job(queued.job_id).telegram_message_id, 9001
        )

    def test_result_rejects_an_arbitrary_outbox_sender_without_partial_commit(self) -> None:
        queued, _ = self.enqueue(543)
        leased = self.state.lease_provider_job("codex", "worker-codex")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(queued.job_id, leased.lease_token)

        with self.assertRaisesRegex(StateError, "sender does not match"):
            self.state.commit_provider_result(
                queued.job_id,
                leased.lease_token,
                visible_response="Visible response",
                sender_agent_id="opencode",
                telegram_html="Visible response",
            )

        self.assertEqual(self.state.get_provider_job(queued.job_id).status, "executing")
        with self.assertRaisesRegex(StateError, "has no result"):
            self.state.get_provider_result(queued.job_id)

    def test_result_can_use_an_explicit_bounded_user_excerpt(self) -> None:
        queued, _ = self.enqueue(545)
        leased = self.state.lease_provider_job("codex", "worker-codex")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(queued.job_id, leased.lease_token)
        self.state.commit_provider_result(
            queued.job_id,
            leased.lease_token,
            visible_response="result",
            sender_agent_id="codex",
            telegram_html="result",
            user_excerpt="safe admitted excerpt",
        )
        excerpt = self.state._connection.execute(
            "SELECT user_excerpt FROM external_turn_excerpts WHERE topic_id = ? ORDER BY turn_id DESC LIMIT 1",
            (self.topic.topic_id,),
        ).fetchone()
        assert excerpt is not None
        self.assertEqual(excerpt["user_excerpt"], "safe admitted excerpt")

    def test_result_bounds_large_prompt_to_latest_visible_excerpt(self) -> None:
        queued, _ = self.enqueue(547)
        leased = self.state.lease_provider_job("codex", "worker-codex")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(queued.job_id, leased.lease_token)
        latest = "latest visible request"
        self.state.commit_provider_result(
            queued.job_id,
            leased.lease_token,
            visible_response="result",
            sender_agent_id="codex",
            telegram_html="result",
            user_excerpt=f"{'context ' * 400}{latest}",
        )
        excerpt = self.state._connection.execute(
            "SELECT user_excerpt FROM external_turn_excerpts "
            "WHERE topic_id = ? ORDER BY turn_id DESC LIMIT 1",
            (self.topic.topic_id,),
        ).fetchone()
        assert excerpt is not None
        self.assertLessEqual(len(excerpt["user_excerpt"]), 2000)
        self.assertTrue(str(excerpt["user_excerpt"]).endswith(latest))

    def test_result_cannot_bind_provider_session_to_a_different_topic_session(self) -> None:
        queued, _ = self.enqueue(545)
        leased = self.state.lease_provider_job("codex", "worker-codex")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(queued.job_id, leased.lease_token)
        other_topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=self.topic.chat_id,
            thread_id=80,
            title="Other binding topic",
        )
        other_session = self.state.activate_agent(
            other_topic.topic_id, "codex", "gpt-example", "high"
        )
        self.state._connection.execute(
            "UPDATE provider_jobs SET session_id = ? WHERE job_id = ?",
            (other_session.session_id, queued.job_id),
        )
        self.state._connection.commit()
        with self.assertRaisesRegex(StateError, "session generation"):
            self.state.commit_provider_result(
                queued.job_id,
                leased.lease_token,
                visible_response="result",
                sender_agent_id="codex",
                telegram_html="result",
                provider_session_id="wrong-topic-provider-session",
            )
        self.assertEqual(self.state.get_session(other_session.session_id).provider_session_id, None)

    def test_pruned_valid_context_watermark_still_commits_and_advances_cursor(self) -> None:
        queued, _ = self.enqueue(546)
        for index in range(100):
            self.state.record_visible_turn(
                self.topic.topic_id,
                agent_id="opencode",
                provider="opencode",
                model="gpt-example",
                user_excerpt=f"later request {index}",
                response_excerpt=f"later response {index}",
            )
        self.assertIsNone(
            self.state._connection.execute(
                "SELECT 1 FROM external_turn_excerpts WHERE turn_id = ?", (self.context_turn_id,)
            ).fetchone()
        )
        leased = self.state.lease_provider_job("codex", "worker-codex")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(queued.job_id, leased.lease_token)
        self.state.commit_provider_result(
            queued.job_id,
            leased.lease_token,
            visible_response="result",
            sender_agent_id="codex",
            telegram_html="result",
            acknowledge_context=True,
        )
        cursor = self.state._connection.execute(
            """SELECT last_turn_id FROM visible_context_cursors
               WHERE topic_id = ? AND observer_agent_id = ?""",
            (self.topic.topic_id, "codex"),
        ).fetchone()
        assert cursor is not None
        self.assertEqual(cursor["last_turn_id"], self.context_turn_id)

    def test_expired_leases_cannot_commit_fail_or_mutate_outbox(self) -> None:
        past = datetime(2026, 1, 1, tzinfo=timezone.utc)
        queued, _ = self.enqueue(546)
        leased = self.state.lease_provider_job("codex", "worker-codex", lease_seconds=1, now=past)
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(queued.job_id, leased.lease_token, now=past)
        expired = past + timedelta(seconds=2)
        with self.assertRaisesRegex(StateError, "lease"):
            self.state.fail_provider_job(
                queued.job_id,
                leased.lease_token,
                error_class="permanent",
                error_code="bad",
                now=expired,
            )
        with self.assertRaisesRegex(StateError, "lease"):
            self.state.commit_provider_result(
                queued.job_id,
                leased.lease_token,
                visible_response="late",
                sender_agent_id="codex",
                telegram_html="late",
                now=expired,
            )

        self.state.recover_stale_provider_jobs(now=expired)
        self.assertEqual(self.state.get_provider_job(queued.job_id).status, "indeterminate")

        ready_topic = self.state.observe_topic(
            project_id="ready-example-project",
            chat_id=self.topic.chat_id,
            thread_id=79,
            title="Ready outbox topic",
        )
        ready_session = self.state.activate_agent(
            ready_topic.topic_id, "codex", "gpt-example", "high"
        )
        ready, _ = self.state.enqueue_provider_job(
            idempotency_key="telegram:-1001234567890:547",
            chat_id=ready_topic.chat_id,
            message_id=547,
            topic_id=ready_topic.topic_id,
            agent_id="codex",
            session_id=ready_session.session_id,
            session_generation=ready_session.generation,
            model="gpt-example",
            effort="high",
            payload_text="ready request",
        )
        ready_lease = self.state.lease_provider_job(
            "codex", "worker-ready", lease_seconds=60, now=past
        )
        assert ready_lease is not None and ready_lease.lease_token is not None
        self.state.mark_provider_job_executing(ready.job_id, ready_lease.lease_token, now=past)
        self.state.commit_provider_result(
            ready.job_id,
            ready_lease.lease_token,
            visible_response="ready",
            sender_agent_id="codex",
            telegram_html="ready",
            now=past,
        )
        outbox = self.state.lease_telegram_outbox("codex", "sender", lease_seconds=1, now=past)
        assert outbox is not None and outbox.lease_token is not None
        with self.assertRaisesRegex(StateError, "lease"):
            self.state.retry_telegram_outbox(
                outbox.outbox_id,
                outbox.lease_token,
                error_code="late",
                delay_seconds=0,
                now=expired,
            )
        with self.assertRaisesRegex(StateError, "lease"):
            self.state.mark_telegram_outbox_delivered(
                outbox.outbox_id, outbox.lease_token, telegram_message_id=9002, now=expired
            )

    def test_terminal_outbox_failure_unblocks_fifo_without_replaying_provider(self) -> None:
        first, _ = self.enqueue(548)
        second, _ = self.enqueue(549)
        first_lease = self.state.lease_provider_job("codex", "worker-one")
        assert first_lease is not None and first_lease.lease_token is not None
        self.state.mark_provider_job_executing(first.job_id, first_lease.lease_token)
        self.state.commit_provider_result(
            first.job_id,
            first_lease.lease_token,
            visible_response="first",
            sender_agent_id="codex",
            telegram_html="first",
        )
        self.state._connection.execute(
            "UPDATE telegram_outbox SET attempt_count = 19 WHERE job_id = ?", (first.job_id,)
        )
        self.state._connection.commit()
        outbox = self.state.lease_telegram_outbox("codex", "sender")
        assert outbox is not None and outbox.lease_token is not None
        terminal = self.state.retry_telegram_outbox(
            outbox.outbox_id, outbox.lease_token, error_code="telegram_unavailable", delay_seconds=0
        )
        self.assertEqual(terminal.status, "failed")
        failed = self.state.get_provider_job(first.job_id)
        self.assertEqual((failed.status, failed.error_class), ("failed", "telegram_delivery"))
        next_job = self.state.lease_provider_job("codex", "worker-two")
        assert next_job is not None
        self.assertEqual(next_job.job_id, second.job_id)

    def test_retry_is_allowed_only_before_execution_and_is_bounded(self) -> None:
        queued, _ = self.enqueue(550)
        leased = self.state.lease_provider_job("codex", "worker-codex")
        assert leased is not None and leased.lease_token is not None
        retry = self.state.schedule_provider_job_retry(
            queued.job_id,
            leased.lease_token,
            error_code="network-before-start",
            delay_seconds=15,
        )
        self.assertEqual(retry.status, "retry_wait")
        with self.assertRaisesRegex(StateError, "pre-execution"):
            self.state.schedule_provider_job_retry(
                queued.job_id,
                leased.lease_token,
                error_code="again",
                delay_seconds=15,
            )

    def test_concurrent_enqueue_assigns_unique_contiguous_topic_sequence(self) -> None:
        session_id = self.codex.session_id
        generation = self.codex.generation
        self.state.close()
        barrier = threading.Barrier(6)
        sequences: list[int] = []
        failures: list[BaseException] = []
        lock = threading.Lock()

        def enqueue(index: int) -> None:
            state = HubState.open(self.path)
            try:
                barrier.wait()
                job, _ = state.enqueue_provider_job(
                    idempotency_key=f"telegram:concurrent:{index}",
                    chat_id=self.topic.chat_id,
                    message_id=600 + index,
                    topic_id=self.topic.topic_id,
                    agent_id="codex",
                    session_id=session_id,
                    session_generation=generation,
                    model="gpt-example",
                    effort="high",
                    payload_text=f"request {index}",
                )
                with lock:
                    sequences.append(job.topic_sequence)
            except BaseException as exc:  # pragma: no cover - diagnostic collection
                with lock:
                    failures.append(exc)
            finally:
                state.close()

        threads = [threading.Thread(target=enqueue, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.state = HubState.open(self.path)

        self.assertEqual(failures, [])
        self.assertEqual(sorted(sequences), [1, 2, 3, 4, 5, 6])

    def test_concurrent_workers_cannot_lease_the_same_job(self) -> None:
        queued, _ = self.enqueue(559)
        self.state.close()
        barrier = threading.Barrier(4)
        claimed: list[str] = []
        failures: list[BaseException] = []
        lock = threading.Lock()

        def lease(index: int) -> None:
            state = HubState.open(self.path)
            try:
                barrier.wait()
                job = state.lease_provider_job("codex", f"worker-{index}")
                if job is not None:
                    with lock:
                        claimed.append(job.job_id)
            except BaseException as exc:  # pragma: no cover - diagnostic collection
                with lock:
                    failures.append(exc)
            finally:
                state.close()

        threads = [threading.Thread(target=lease, args=(index,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.state = HubState.open(self.path)

        self.assertEqual(failures, [])
        self.assertEqual(claimed, [queued.job_id])

    def test_queue_state_survives_close_and_reopen(self) -> None:
        queued, _ = self.enqueue(560)
        self.state.close()
        self.state = HubState.open(self.path)
        reopened = self.state.get_provider_job(queued.job_id)
        self.assertEqual((reopened.status, reopened.topic_sequence), ("queued", 1))


if __name__ == "__main__":
    unittest.main()
