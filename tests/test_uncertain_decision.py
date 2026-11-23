"""ADR 0107: the owner's one-tap decision for an uncertain Claude job.

Fictional state, fictional Telegram; no provider is invoked.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from hermes_codex_router import artifact_capabilities
from hermes_codex_router.codex_failure import uncertain_provider_notice
from hermes_codex_router.final_delivery import deliver_final_part
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.uncertain_decision import (
    CONTINUATION_PROMPT,
    PROMPT_HTML,
    keyboard,
    parse_callback,
    parse_prompt_part,
    prompt_part_html,
    snapshot_binding,
)
from hermes_codex_router.uncertain_decision_state import UncertainDecisionState

CHAT = -1001234567890
THREAD = 77
OWNER = 424242


def enabled(value: bool = True) -> Any:
    return mock.patch.object(artifact_capabilities, "REMOTE_UNCERTAIN_DECISION", value)


class _StateFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        base = Path(self.tempdir.name)
        self.root = (base / "project").resolve()
        self.root.mkdir()
        self.state_path = base / "private" / "hub.db"
        self.state = HubState.open(self.state_path, codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=CHAT,
            thread_id=THREAD,
            title="Example topic",
            execution_root=self.root,
        )
        self.session = self.state.activate_agent(
            self.topic.topic_id, "claude", "claude-example", "high"
        )
        self.state.bind_provider_session(
            self.session.session_id, "00000000-0000-4000-8000-000000000001", None
        )

    def enqueue(self, message_id: int) -> Any:
        job, _ = self.state.enqueue_provider_job(
            idempotency_key=f"telegram:{CHAT}:{message_id}",
            chat_id=CHAT,
            message_id=message_id,
            topic_id=self.topic.topic_id,
            agent_id="claude",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            provider_session_id=None,
            model="claude-example",
            effort="high",
            payload_text=f"bounded request {message_id}",
            context_watermark=None,
        )
        return job

    def uncertain(self, message_id: int = 10, *, prompt: bool = True, follower: int = 0) -> Any:
        job = self.enqueue(message_id)
        if follower:
            self.follower = self.enqueue(follower)
        leased = self.state.lease_provider_job("claude", "worker-claude")
        assert leased is not None and leased.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, leased.lease_token)
        self.state.terminate_provider_job_with_notice(
            job.job_id,
            leased.lease_token,
            status="indeterminate",
            error_class="ambiguous_execution",
            error_code="ClaudeStreamError",
            sender_agent_id="claude",
            telegram_html=uncertain_provider_notice("Claude", job.job_id, decision_buttons=prompt),
            decision_prompt=prompt,
        )
        return self.state.get_provider_job(job.job_id)

    def parts(self, job_id: str) -> list[sqlite3.Row]:
        return self.state._connection.execute(
            """SELECT parts.* FROM telegram_outbox_parts parts
               JOIN telegram_outbox outbox ON outbox.outbox_id = parts.outbox_id
               WHERE outbox.job_id = ? ORDER BY parts.part_index""",
            (job_id,),
        ).fetchall()

    def deliver_prompt(self, job_id: str, message_id: int = 900) -> str:
        prompt = self.parts(job_id)[-1]
        snapshot = parse_prompt_part(str(prompt["telegram_html"]))
        assert snapshot is not None
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE telegram_outbox_parts SET telegram_message_id=?, "
                "receipt_validation_version=1 WHERE outbox_id=? AND part_index=?",
                (message_id, prompt["outbox_id"], prompt["part_index"]),
            )
        return snapshot

    def decide(self, action: str, snapshot: str, **overrides: Any) -> Any:
        values: dict[str, Any] = {
            "action": action,
            "snapshot": snapshot,
            "actor_user_id": OWNER,
            "callback_id": "callback-1",
            "chat_id": CHAT,
            "thread_id": THREAD,
            "prompt_message_id": 900,
            "canonical_root": self.root,
        }
        values.update(overrides)
        return UncertainDecisionState(self.state).decide(**values)


class UncertainDecisionStateTests(_StateFixture):
    def test_prompt_follows_the_notice_only_when_enabled_and_requested(self) -> None:
        with enabled():
            job = self.uncertain(10)
        parts = self.parts(job.job_id)
        self.assertEqual(job.status, "indeterminate")
        self.assertIn("кнопкой", parts[0]["telegram_html"])
        self.assertNotIn("indeterminate-resolve", parts[0]["telegram_html"])
        self.assertEqual(
            parse_prompt_part(parts[-1]["telegram_html"]),
            snapshot_binding(
                job.job_id,
                self.session.session_id,
                self.session.generation,
                "00000000-0000-4000-8000-000000000001",
            ),
        )
        with enabled(False), self.assertRaisesRegex(StateError, "выключено"):
            self.decide("close", parse_prompt_part(parts[-1]["telegram_html"]) or "")

    def test_disabled_artifact_never_attaches_a_prompt(self) -> None:
        with enabled(False):
            job = self.uncertain(11)
        parts = self.parts(job.job_id)
        self.assertTrue(all(parse_prompt_part(p["telegram_html"]) is None for p in parts))

    def test_close_records_resolution_and_decision_and_releases_root(self) -> None:
        with enabled():
            job = self.uncertain(12, follower=13)
            follower = self.follower
            snapshot = self.deliver_prompt(job.job_id)
            self.assertIsNone(self.state.lease_provider_job("claude", "worker-claude"))
            decision = self.decide("close", snapshot)
            repeated = self.decide("continue", snapshot, callback_id="callback-2")
        self.assertTrue(decision.created)
        self.assertIsNone(decision.continuation_job_id)
        self.assertFalse(repeated.created)
        self.assertEqual(repeated.action, "close")
        row = self.state._connection.execute(
            "SELECT * FROM provider_job_owner_decisions WHERE job_id=?", (job.job_id,)
        ).fetchone()
        self.assertEqual(row["decision"], "close_without_retry")
        self.assertEqual(row["authority"], "telegram_owner_callback")
        self.assertEqual(row["actor_user_id"], OWNER)
        self.assertEqual(row["prompt_message_id"], 900)
        self.assertEqual(row["session_generation"], self.session.generation)
        self.assertEqual(self.state.get_provider_job(job.job_id).status, "indeterminate")
        leased = self.state.lease_provider_job("claude", "worker-claude")
        self.assertIsNotNone(leased)
        assert leased is not None
        self.assertEqual(leased.job_id, follower.job_id)

    def test_continue_creates_one_inspection_job_ahead_of_the_held_tail(self) -> None:
        with enabled():
            job = self.uncertain(14, follower=15)
            follower = self.follower
            snapshot = self.deliver_prompt(job.job_id)
            decision = self.decide("continue", snapshot)
            repeated = self.decide("continue", snapshot, callback_id="callback-2")
        self.assertTrue(decision.created)
        self.assertEqual(decision.held_count, 1)
        self.assertEqual(repeated.continuation_job_id, decision.continuation_job_id)
        self.assertFalse(repeated.created)
        assert decision.continuation_job_id is not None
        continuation = self.state.get_provider_job(decision.continuation_job_id)
        self.assertEqual(continuation.payload_text, CONTINUATION_PROMPT)
        self.assertEqual(continuation.session_id, self.session.session_id)
        self.assertEqual(continuation.session_generation, self.session.generation)
        self.assertEqual(continuation.provider_session_id, "00000000-0000-4000-8000-000000000001")
        self.assertEqual(continuation.max_attempts, 1)
        leased = self.state.lease_provider_job("claude", "worker-claude")
        assert leased is not None
        self.assertEqual(leased.job_id, continuation.job_id)
        held = self.state._connection.execute(
            "SELECT decision FROM provider_job_holds WHERE job_id=?", (follower.job_id,)
        ).fetchone()
        self.assertEqual(held["decision"], "pending")
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM provider_jobs WHERE idempotency_key LIKE 'continuation:%'"
            ).fetchone()[0],
            1,
        )

    def test_continue_refuses_when_a_confirmed_request_would_run_first(self) -> None:
        with enabled():
            job = self.uncertain(60, follower=61)
            snapshot = self.deliver_prompt(job.job_id)
            with self.state._connection:
                self.state._connection.execute(
                    """INSERT INTO provider_job_holds (job_id, cause_job_id, held_at, decision)
                       VALUES (?, ?, 'now', 'confirmed')""",
                    (self.follower.job_id, job.job_id),
                )
            with self.assertRaisesRegex(StateError, "Закрыть без повтора"):
                self.decide("continue", snapshot)
            self.assertIsNone(
                self.state._connection.execute(
                    "SELECT 1 FROM provider_job_resolutions WHERE job_id=?", (job.job_id,)
                ).fetchone()
            )
            self.assertEqual(
                self.state._connection.execute(
                    "SELECT COUNT(*) FROM provider_jobs WHERE idempotency_key LIKE 'continuation:%'"
                ).fetchone()[0],
                0,
            )
            self.assertTrue(self.decide("close", snapshot).created)

    def test_only_the_last_notice_part_can_be_the_prompt(self) -> None:
        with enabled():
            job = self.uncertain(62)
            snapshot = self.deliver_prompt(job.job_id)
            prompt = self.parts(job.job_id)[-1]
            with self.state._connection:
                self.state._connection.execute(
                    """INSERT INTO telegram_outbox_parts
                       (outbox_id, part_index, telegram_html, part_type)
                       VALUES (?, ?, 'later text', 'text')""",
                    (prompt["outbox_id"], int(prompt["part_index"]) + 1),
                )
            with self.assertRaisesRegex(StateError, "не доставленный"):
                self.decide("close", snapshot)

    def test_stale_or_foreign_presses_change_nothing(self) -> None:
        with enabled():
            job = self.uncertain(16)
            snapshot = self.deliver_prompt(job.job_id)
            other = "0" * 16 if snapshot != "0" * 16 else "1" * 16
            refusals = (
                {"snapshot": other},
                {"prompt_message_id": 901},
                {"chat_id": CHAT - 1},
                {"thread_id": THREAD + 1},
            )
            for override in refusals:
                with self.subTest(override=override):
                    values = {"snapshot": snapshot, **override}
                    with self.assertRaises(StateError):
                        self.decide("close", **values)
        self.assertIsNone(
            self.state._connection.execute(
                "SELECT 1 FROM provider_job_resolutions WHERE job_id=?", (job.job_id,)
            ).fetchone()
        )

    def test_undelivered_prompt_cannot_be_pressed(self) -> None:
        with enabled():
            job = self.uncertain(17)
            snapshot = parse_prompt_part(self.parts(job.job_id)[-1]["telegram_html"]) or ""
            with self.assertRaisesRegex(StateError, "не доставленный"):
                self.decide("close", snapshot)

    def test_new_session_generation_makes_the_prompt_stale(self) -> None:
        with enabled():
            job = self.uncertain(18)
            snapshot = self.deliver_prompt(job.job_id)
            with self.state._connection:
                self.state._connection.execute(
                    "UPDATE agent_sessions SET generation = generation + 1 WHERE session_id=?",
                    (self.session.session_id,),
                )
            with self.assertRaisesRegex(StateError, "устарели"):
                self.decide("continue", snapshot)

    def test_local_resolution_first_makes_the_buttons_refuse(self) -> None:
        with enabled():
            job = self.uncertain(19)
            snapshot = self.deliver_prompt(job.job_id)
            self.assertTrue(self.state.resolve_indeterminate_job(job.job_id, "acknowledged"))
            with self.assertRaisesRegex(StateError, "другим способом"):
                self.decide("close", snapshot)

    def test_continue_refuses_while_another_job_in_the_root_is_uncertain(self) -> None:
        with enabled():
            job = self.uncertain(20)
            snapshot = self.deliver_prompt(job.job_id)
            other_topic = self.state.observe_topic(
                project_id="example-project",
                chat_id=CHAT,
                thread_id=THREAD + 1,
                title="Second topic",
                execution_root=self.root,
            )
            other_session = self.state.activate_agent(
                other_topic.topic_id, "claude", "claude-example", "high"
            )
            with self.state._connection:
                self.state._connection.execute(
                    """INSERT INTO provider_jobs
                       (job_id, idempotency_key, chat_id, message_id, topic_id, topic_sequence,
                        agent_id, session_id, session_generation, model, effort, payload_text,
                        status, created_at, updated_at)
                       VALUES ('other-uncertain', 'other-key', ?, 99, ?, 1, 'claude', ?, ?,
                               'claude-example', 'high', 'other', 'indeterminate', 'now', 'now')""",
                    (
                        CHAT,
                        other_topic.topic_id,
                        other_session.session_id,
                        other_session.generation,
                    ),
                )
            with self.assertRaisesRegex(StateError, "другая активная"):
                self.decide("continue", snapshot)
            self.assertTrue(self.decide("close", snapshot).created)

    def test_decision_ledger_is_append_only_and_bound_to_its_resolution(self) -> None:
        with enabled():
            job = self.uncertain(21)
            snapshot = self.deliver_prompt(job.job_id)
            self.decide("close", snapshot)
        connection = self.state._connection
        with self.assertRaisesRegex(sqlite3.DatabaseError, "immutable"):
            with connection:
                connection.execute("UPDATE provider_job_owner_decisions SET actor_user_id=1")
        with self.assertRaisesRegex(sqlite3.DatabaseError, "cannot be deleted"):
            with connection:
                connection.execute("DELETE FROM provider_job_owner_decisions")
        unresolved = self.uncertain(22, prompt=False)
        with self.assertRaisesRegex(sqlite3.DatabaseError, "needs its resolution"):
            with connection:
                connection.execute(
                    """INSERT INTO provider_job_owner_decisions
                       (job_id, decision, authority, actor_user_id, callback_id, outbox_id,
                        prompt_part_index, chat_id, thread_id, prompt_message_id, session_id,
                        session_generation, snapshot, continuation_job_id, decided_at)
                       VALUES (?, 'close_without_retry', 'telegram_owner_callback', 1, 'x',
                               'outbox', 2, ?, ?, 5, ?, 1, ?, NULL, 'now')""",
                    (unresolved.job_id, CHAT, THREAD, self.session.session_id, "a" * 16),
                )

    def test_correction_pair_gets_no_prompt(self) -> None:
        with enabled():
            job = self.enqueue(23)
            with mock.patch.object(UncertainDecisionState, "correction_linked", return_value=True):
                leased = self.state.lease_provider_job("claude", "worker-claude")
                assert leased is not None and leased.lease_token is not None
                self.state.mark_provider_job_executing(job.job_id, leased.lease_token)
                self.state.terminate_provider_job_with_notice(
                    job.job_id,
                    leased.lease_token,
                    status="indeterminate",
                    error_class="ambiguous_execution",
                    error_code="ClaudeStreamError",
                    sender_agent_id="claude",
                    telegram_html="notice",
                    decision_prompt=True,
                )
        self.assertTrue(
            all(parse_prompt_part(p["telegram_html"]) is None for p in self.parts(job.job_id))
        )


class FakeBot:
    def __init__(self, first_id: int) -> None:
        self.sent: list[tuple[str, object]] = []
        self.next_id = first_id

    def send_html(self, chat_id: int, thread_id: int, html: str, **kwargs: Any) -> int:
        self.sent.append((html, kwargs.get("reply_markup")))
        self.next_id += 1
        return self.next_id

    def send_document(self, *args: Any, **kwargs: Any) -> int:
        raise AssertionError("no documents in this notice")


class PromptDeliveryTests(_StateFixture):
    def deliver_all(self, hub: FakeBot | None) -> FakeBot:
        provider = FakeBot(100)
        with enabled():
            job = self.uncertain(30)
        for _ in range(len(self.parts(job.job_id))):
            outbox = self.state.lease_telegram_outbox("claude", "sender")
            assert outbox is not None
            deliver_final_part(
                self.state.delivery,
                provider,
                outbox,
                state_path=self.state_path,
                choice_telegram=hub,
            )
        self.job_id = job.job_id
        return provider

    def test_prompt_is_sent_by_the_hub_bot_after_the_notice(self) -> None:
        hub = FakeBot(800)
        provider = self.deliver_all(hub)
        self.assertEqual(len(provider.sent), 1)
        self.assertIn("кнопкой", provider.sent[0][0])
        snapshot = parse_prompt_part(self.parts(self.job_id)[-1]["telegram_html"])
        assert snapshot is not None
        self.assertEqual(hub.sent, [(PROMPT_HTML, keyboard(snapshot))])
        self.assertEqual(self.state.get_telegram_outbox_for_job(self.job_id).status, "delivered")

    def test_without_hub_bot_the_prompt_degrades_to_the_local_command(self) -> None:
        provider = self.deliver_all(None)
        self.assertEqual(len(provider.sent), 2)
        self.assertIn("indeterminate-resolve", provider.sent[1][0])
        self.assertIsNone(provider.sent[1][1])


class PureHelperTests(unittest.TestCase):
    def test_markers_and_callbacks_are_strict(self) -> None:
        snapshot = snapshot_binding("job", "session", 1, None)
        self.assertEqual(parse_prompt_part(prompt_part_html(snapshot)), snapshot)
        self.assertIsNone(parse_prompt_part(prompt_part_html(snapshot) + "x"))
        with self.assertRaises(ValueError):
            prompt_part_html("XYZ")
        data = [button[0]["callback_data"] for button in keyboard(snapshot)["inline_keyboard"]]  # type: ignore[index]
        self.assertEqual(
            [parse_callback(item) for item in data],
            [("continue", snapshot), ("close", snapshot)],
        )
        self.assertTrue(all(len(item.encode()) <= 64 for item in data))
        self.assertIsNone(parse_callback("ud:c:" + snapshot.upper()))
        self.assertIsNone(parse_callback("ud:done"))

    def test_notice_and_prompt_copy_stay_short_and_honest(self) -> None:
        notice = uncertain_provider_notice("Claude", "job-1", decision_buttons=True)
        self.assertLessEqual(len(notice), 400)
        self.assertIn("Автоповтора не будет", notice)
        self.assertLessEqual(len(PROMPT_HTML), 400)
        self.assertIn("не повторится", PROMPT_HTML)


if __name__ == "__main__":
    unittest.main()


class FakeTelegram:
    def __init__(self) -> None:
        self.answers: list[str] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.answers.append(text or "")

    def call(self, method: str, **kwargs: Any) -> Any:
        self.calls.append((method, kwargs))
        return True


class CallbackHandlerTests(_StateFixture):
    HUB_BOT = 777000

    def service(self, *, enabled_config: bool = True) -> Any:
        from types import SimpleNamespace

        root = self.root
        config = SimpleNamespace(
            owner_user_ids=(OWNER,),
            hub_bot=SimpleNamespace(telegram_user_id=self.HUB_BOT),
            hub_uncertain_decision_enabled=enabled_config,
            project_for_chat=lambda chat_id: SimpleNamespace(project_id="example-project"),
        )
        registry = SimpleNamespace(require_project=lambda project_id: SimpleNamespace(root=root))
        return SimpleNamespace(
            config=config, registry=registry, state=self.state, telegram=FakeTelegram()
        )

    def press(self, service: Any, data: str, *, sender: int = OWNER, author: Any = HUB_BOT) -> None:
        from hermes_codex_router.owner_decision_callback import handle_owner_decision_callback
        from hermes_codex_router.telegram import TopicCallback

        callback = TopicCallback(
            callback_id=f"cb-{len(service.telegram.answers)}",
            message_id=900,
            chat_id=CHAT,
            thread_id=THREAD,
            sender_id=sender,
            data=data,
            message_sender_id=author,
        )
        self.assertTrue(handle_owner_decision_callback(service, callback))

    def test_owner_press_decides_marks_buttons_and_repeats_safely(self) -> None:
        with enabled():
            job = self.uncertain(40)
            snapshot = self.deliver_prompt(job.job_id)
            service = self.service()
            self.press(service, f"ud:x:{snapshot}")
            self.press(service, f"ud:c:{snapshot}")
            self.press(service, "ud:done")
        answers = service.telegram.answers
        self.assertEqual(answers[0], "Закрыто без повтора; пауза от этой задачи снята")
        self.assertEqual(answers[1], "Решение уже принято: закрыто без повтора")
        self.assertEqual(answers[2], "Решение уже принято")
        method, kwargs = service.telegram.calls[0]
        self.assertEqual(method, "editMessageReplyMarkup")
        self.assertIn("✓ Закрыть без повтора", kwargs["reply_markup"])
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM provider_job_owner_decisions"
            ).fetchone()[0],
            1,
        )

    def test_non_owner_foreign_author_or_disabled_config_cannot_decide(self) -> None:
        with enabled():
            job = self.uncertain(41)
            snapshot = self.deliver_prompt(job.job_id)
            service = self.service()
            self.press(service, f"ud:c:{snapshot}", sender=OWNER + 1)
            self.press(service, f"ud:c:{snapshot}", author=self.HUB_BOT + 1)
            self.press(service, f"ud:c:{snapshot}", author=None)
            self.press(self.service(enabled_config=False), f"ud:c:{snapshot}")
            self.press(service, "ud:c:not-a-snapshot")
        self.assertIn("Только владелец", service.telegram.answers[0])
        self.assertTrue(all("локальная команда" in a for a in service.telegram.answers[1:]))
        self.assertEqual(service.telegram.calls, [])
        self.assertIsNone(
            self.state._connection.execute(
                "SELECT 1 FROM provider_job_resolutions WHERE job_id=?", (job.job_id,)
            ).fetchone()
        )

    def test_state_refusal_is_shown_without_effects(self) -> None:
        with enabled():
            job = self.uncertain(42)
            snapshot = self.deliver_prompt(job.job_id)
            self.state.resolve_indeterminate_job(job.job_id, "acknowledged")
            service = self.service()
            self.press(service, f"ud:c:{snapshot}")
        self.assertEqual(service.telegram.answers, ["Задача уже решена другим способом"])
        self.assertEqual(service.telegram.calls, [])


class OwnedExitProofTests(unittest.TestCase):
    def test_unreaped_or_active_process_withholds_the_proof(self) -> None:
        from hermes_codex_router.external_runtime import ExternalCliAdapter

        adapter = ExternalCliAdapter("claude")
        self.assertTrue(adapter.confirm_owned_claude_exit())
        adapter._claude_unreaped = mock.sentinel.process  # type: ignore[assignment]
        self.assertFalse(adapter.confirm_owned_claude_exit())
        adapter._claude_unreaped = None
        adapter._active_process = mock.sentinel.process  # type: ignore[assignment]
        self.assertFalse(adapter.confirm_owned_claude_exit())
        self.assertFalse(ExternalCliAdapter("opencode").confirm_owned_claude_exit())

    def test_real_group_is_killed_and_reaped_before_the_proof(self) -> None:
        import subprocess

        from hermes_codex_router.external_runtime import ExternalCliAdapter

        adapter = ExternalCliAdapter("claude")
        process = subprocess.Popen(
            ["sleep", "30"], start_new_session=True, stdout=subprocess.DEVNULL
        )
        try:
            adapter._terminate_claude_process(process, graceful=False)  # type: ignore[arg-type]
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        self.assertIsNotNone(process.returncode)
        self.assertTrue(adapter.confirm_owned_claude_exit())

    def test_wait_timeout_keeps_the_proof_withheld(self) -> None:
        import subprocess

        from hermes_codex_router import external_runtime
        from hermes_codex_router.external_runtime import ExternalCliAdapter

        adapter = ExternalCliAdapter("claude")
        process = mock.Mock(returncode=None, pid=999_999_999)
        process.wait.side_effect = subprocess.TimeoutExpired("claude", 5)
        with (
            mock.patch.object(external_runtime, "peek_exit_code", return_value=None),
            mock.patch.object(external_runtime.os, "killpg") as killpg,
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            adapter._terminate_claude_process(process, graceful=False)
        killpg.assert_called_once()
        self.assertFalse(adapter.confirm_owned_claude_exit())

    def test_group_kill_takes_the_descendant_with_the_leader(self) -> None:
        import os
        import subprocess
        import time

        from hermes_codex_router.external_runtime import ExternalCliAdapter

        adapter = ExternalCliAdapter("claude")
        process = subprocess.Popen(
            ["sh", "-c", "sleep 30 & echo $!; wait"],
            start_new_session=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert process.stdout is not None
        child = int(process.stdout.readline())
        try:
            adapter._terminate_claude_process(process, graceful=False)  # type: ignore[arg-type]
        finally:
            process.stdout.close()
        deadline = time.monotonic() + 5
        alive = True
        while alive and time.monotonic() < deadline:
            try:
                with open(f"/proc/{child}/stat") as stat:
                    alive = stat.read().split(") ", 1)[1][0] != "Z"
            except (FileNotFoundError, ProcessLookupError):
                # A child reaped between open() and read() fails the read with ESRCH.
                alive = False
            if alive:
                time.sleep(0.05)
        if alive:
            os.kill(child, 9)
        self.assertFalse(alive)
        self.assertTrue(adapter.confirm_owned_claude_exit())

    def test_host_scope_must_be_observed_inactive(self) -> None:
        from hermes_codex_router.claude_host_profile import ClaudeHostScopes

        scopes = ClaudeHostScopes(lambda code, message: RuntimeError(code))
        self.assertTrue(scopes.confirm_ended())
        scopes.last_unit = "hub-claude-host-0123456789ab.scope"
        with (
            mock.patch.object(scopes, "_resolve"),
            mock.patch.object(scopes, "_cleanup", return_value=False) as cleanup,
        ):
            self.assertFalse(scopes.confirm_ended())
        cleanup.assert_called_once_with("hub-claude-host-0123456789ab.scope")
        scopes.pending.add("hub-claude-host-ba9876543210.scope")
        with (
            mock.patch.object(scopes, "_resolve"),
            mock.patch.object(scopes, "_cleanup", return_value=True),
        ):
            self.assertTrue(scopes.confirm_ended())
        self.assertEqual(scopes.pending, set())
        with mock.patch.object(scopes, "_resolve", side_effect=RuntimeError("missing")):
            self.assertFalse(scopes.confirm_ended())


class WorkerOfferTests(_StateFixture):
    def worker(self, *, runtime: str = "claude", config_on: bool = True, adapter: Any) -> Any:
        from types import SimpleNamespace

        from hermes_codex_router.external_worker import ExternalQueueWorker

        worker = ExternalQueueWorker.__new__(ExternalQueueWorker)
        worker.agent = SimpleNamespace(runtime=runtime)  # type: ignore[assignment]
        worker.config = SimpleNamespace(hub_uncertain_decision_enabled=config_on)  # type: ignore[assignment]
        worker.state = self.state
        worker.adapter = adapter
        return worker

    def test_offer_requires_enabled_claude_and_a_positive_exit_proof(self) -> None:
        from types import SimpleNamespace

        job = self.enqueue(50)
        proven = SimpleNamespace(confirm_owned_claude_exit=lambda: True)
        unproven = SimpleNamespace(confirm_owned_claude_exit=lambda: False)

        def broken() -> bool:
            raise OSError("systemctl unavailable")

        self.assertTrue(self.worker(adapter=proven)._offers_decision(job))
        self.assertFalse(self.worker(adapter=unproven)._offers_decision(job))
        self.assertFalse(self.worker(adapter=SimpleNamespace())._offers_decision(job))
        self.assertFalse(
            self.worker(adapter=SimpleNamespace(confirm_owned_claude_exit=broken))._offers_decision(
                job
            )
        )
        self.assertFalse(self.worker(adapter=proven, config_on=False)._offers_decision(job))
        self.assertFalse(self.worker(adapter=proven, runtime="opencode")._offers_decision(job))
        with mock.patch.object(UncertainDecisionState, "correction_linked", return_value=True):
            self.assertFalse(self.worker(adapter=proven)._offers_decision(job))
