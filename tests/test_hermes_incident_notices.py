from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from hermes_codex_router.hermes_incident_notices import (
    HermesIncidentNotices,
    NoticeRejected,
    NoticeSettings,
    file_call,
)
from hermes_codex_router.incident_journal import IncidentJournal
from hermes_codex_router.operational_alert import OperationalAlert

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
OWNER = 123456789


class IncidentNoticeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.journal = self.root / "incident-journal.json"
        self.cursor = self.root / "hermes-incident-cursor.json"
        self.settings = NoticeSettings(self.journal, self.cursor, OWNER)
        self.consumer = HermesIncidentNotices(self.settings)
        self.sent: list[tuple[str, str]] = []

    def project(self, keys: tuple[str, ...], now: datetime = NOW) -> None:
        alerts = tuple(
            OperationalAlert(key, "provider_work_stalled", "warning", f"Safe trigger {key}")
            for key in keys
        )
        journal = IncidentJournal(self.journal)
        with journal.cycle():
            journal.project(alerts, now=now)

    async def send(self, text: str, token: str) -> int:
        self.sent.append((text, token))
        return len(self.sent)

    async def tick(self, seconds: int = 0) -> str:
        return await self.consumer.tick(self.send, now=NOW + timedelta(seconds=seconds))

    def saved(self) -> dict:
        return json.loads(self.cursor.read_text())

    async def test_cold_start_is_one_summary_without_historical_cards(self) -> None:
        self.project(tuple(f"example:{n}" for n in range(40)))
        self.assertEqual(await self.tick(), "sent")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("40", self.sent[0][0])
        self.assertEqual(await self.tick(1), "idle")
        self.assertEqual(self.cursor.stat().st_mode & 0o777, 0o600)

    async def test_new_episode_resolution_and_recurrence_have_one_notice_each(self) -> None:
        self.project(())
        await self.tick()
        self.project(("example:a",), NOW + timedelta(seconds=1))
        await self.tick(1)
        self.assertIn("Safe trigger example:a", self.sent[1][0])
        self.assertIn("Inspect the exact saved outcome", self.sent[1][0])
        self.project((), NOW + timedelta(seconds=2))
        await self.tick(2)
        self.assertIn("закрыт", self.sent[2][0])
        self.project(("example:a",), NOW + timedelta(seconds=301))
        await self.tick(301)
        self.assertEqual(len(self.sent), 4)

    async def test_short_episode_closed_between_polls_is_still_reported_once(self) -> None:
        self.project(())
        await self.tick()
        self.project(("example:a",), NOW + timedelta(seconds=1))
        self.project((), NOW + timedelta(seconds=2))
        await self.tick(3)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("Safe trigger example:a", self.sent[1][0])
        self.assertIn("Уже наблюдалось закрытие", self.sent[1][0])
        await self.tick(4)
        self.assertEqual(len(self.sent), 2)

    async def test_unknown_send_and_restart_never_resend(self) -> None:
        self.project(("example:a",))

        async def ambiguous(text: str, token: str) -> int:
            self.assertEqual(self.saved()["deliveries"][-1]["state"], "attempted")
            raise OSError("private diagnostic must not enter cursor")

        self.assertEqual(await self.consumer.tick(ambiguous, now=NOW), "unknown")
        self.assertNotIn("private diagnostic", self.cursor.read_text())
        self.consumer = HermesIncidentNotices(self.settings)
        self.assertEqual(await self.tick(1), "idle")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.saved()["deliveries"][-1]["state"], "unknown")

    async def test_positive_receipt_and_cursor_commit_fault_do_not_resend(self) -> None:
        self.project(())
        original = self.consumer.store.write

        async def accepted(text: str, token: str) -> int:
            self.consumer.store.write = mock.Mock(side_effect=OSError("disk"))
            return 8

        self.assertEqual(await self.consumer.tick(accepted, now=NOW), "unavailable")
        self.consumer.store.write = original
        self.consumer = HermesIncidentNotices(self.settings)
        self.assertEqual(await self.tick(1), "idle")
        self.assertEqual(self.saved()["deliveries"][-1]["state"], "unknown")

    async def test_cancellation_preserves_attempted_or_unknown_fence(self) -> None:
        self.project(())

        async def cancelled(text: str, token: str) -> int:
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.consumer.tick(cancelled, now=NOW)
        self.consumer = HermesIncidentNotices(self.settings)
        await self.tick(1)
        self.assertEqual(self.sent, [])

    async def test_only_proven_rejection_retries_after_its_deadline(self) -> None:
        self.project(())

        async def rejected(text: str, token: str) -> int:
            raise NoticeRejected(20)

        self.assertEqual(await self.consumer.tick(rejected, now=NOW), "rejected")
        self.assertEqual(await self.tick(19), "idle")
        self.assertEqual(await self.tick(20), "sent")
        self.assertEqual(len(self.sent), 1)

    async def test_malformed_or_bool_receipt_is_unknown(self) -> None:
        for receipt in (None, True, 0, "1"):
            with self.subTest(receipt=receipt):
                self.cursor.unlink(missing_ok=True)
                self.project(())

                async def send(text: str, token: str):
                    return receipt

                self.assertEqual(await self.consumer.tick(send, now=NOW), "unknown")
                await self.tick(1)
        self.assertEqual(self.sent, [])

    async def test_storm_summarizes_once_and_is_bounded_per_window(self) -> None:
        self.project(())
        await self.tick()
        self.project(tuple(f"example:{n}" for n in range(30)), NOW + timedelta(seconds=1))
        await self.tick(1)
        self.assertLessEqual(len(self.sent), self.settings.max_cards)
        self.assertIn("Сводка", self.sent[-1][0])
        await self.tick(2)
        self.assertLessEqual(len(self.sent), self.settings.max_cards)
        self.assertEqual(len(self.saved()["seen"]), 30)

    async def test_silence_and_recovery_are_edge_triggered(self) -> None:
        self.project(())
        await self.tick()
        await self.tick(180)
        await self.tick(181)
        self.assertEqual(len(self.sent), 2)
        self.assertIn("не обновляется", self.sent[-1][0])
        self.project((), NOW + timedelta(seconds=301))
        await self.tick(301)
        self.assertIn("обновляется", self.sent[-1][0])
        await self.tick(302)
        self.assertEqual(len(self.sent), 3)

    async def test_unsafe_journal_does_not_invent_resolution_or_send_content(self) -> None:
        self.project(("example:a",))
        await self.tick()
        before = self.saved()["seen"]
        self.journal.chmod(0o644)
        await self.tick(1)
        self.assertEqual(self.saved()["seen"], before)
        self.assertIn("недоступен", self.sent[-1][0])

    async def test_symlink_or_unsafe_cursor_is_preserved(self) -> None:
        self.project(())
        foreign = self.root / "example.txt"
        foreign.write_text("preserve")
        self.cursor.symlink_to(foreign)
        self.assertEqual(await self.tick(), "unavailable")
        self.assertEqual(foreign.read_text(), "preserve")
        self.assertEqual(self.sent, [])

    async def test_destination_change_refuses_existing_cursor(self) -> None:
        self.project(())
        await self.tick()
        before = self.cursor.read_bytes()
        changed = NoticeSettings(self.journal, self.cursor, OWNER + 1)
        self.consumer = HermesIncidentNotices(changed)
        self.assertEqual(await self.tick(1), "unavailable")
        self.assertEqual(self.cursor.read_bytes(), before)

    async def test_dismiss_requires_exact_owner_chat_token_and_receipted_message(self) -> None:
        self.project(("example:a",))
        await self.tick()
        token = self.sent[0][1]
        for actor, chat, message, key in (
            (OWNER + 1, OWNER, 1, token),
            (OWNER, OWNER + 1, 1, token),
            (OWNER, OWNER, 2, token),
            (OWNER, OWNER, 1, "unknown"),
            (True, OWNER, 1, token),
        ):
            self.assertFalse(self.consumer.dismiss(key, actor, chat, message))
        self.assertTrue(self.consumer.dismiss(token, OWNER, OWNER, 1))
        self.assertTrue(self.consumer.dismiss(token, OWNER, OWNER, 1))
        self.project((), NOW + timedelta(seconds=1))
        await self.tick(1)
        self.assertEqual(len(self.sent), 1)

    async def test_concurrent_tick_cannot_duplicate_a_send(self) -> None:
        self.project(())
        entered = asyncio.Event()
        release = asyncio.Event()

        async def waiting(text: str, token: str) -> int:
            entered.set()
            await release.wait()
            return 1

        first = asyncio.create_task(self.consumer.tick(waiting, now=NOW))
        await entered.wait()
        self.assertEqual(
            await HermesIncidentNotices(self.settings).tick(self.send, now=NOW), "busy"
        )
        release.set()
        self.assertEqual(await first, "sent")

    async def test_clock_regression_and_future_journal_refuse_without_changes(self) -> None:
        self.project(())
        await self.tick(10)
        before = self.cursor.read_bytes()
        self.assertEqual(await self.tick(9), "unavailable")
        self.assertEqual(self.cursor.read_bytes(), before)
        self.project((), NOW + timedelta(seconds=100))
        self.assertEqual(await self.tick(11), "unavailable")

    async def test_three_proven_rejections_are_exhausted_not_unknown(self) -> None:
        self.project(())

        async def rejected(text: str, token: str) -> int:
            raise NoticeRejected(1)

        for second in (0, 1, 2):
            self.assertEqual(
                await self.consumer.tick(rejected, now=NOW + timedelta(seconds=second)), "rejected"
            )
        self.assertEqual(self.saved()["deliveries"][0]["state"], "rejected")
        await self.tick(301)
        self.assertNotIn(self.saved()["deliveries"][0]["token"], [t for _, t in self.sent])

    async def test_later_success_cannot_hide_unknown_outcome(self) -> None:
        self.project(())
        await self.tick()
        self.project(("example:a", "example:b"), NOW + timedelta(seconds=1))
        calls = 0

        async def mixed(text: str, token: str) -> int:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("ambiguous")
            return 9

        self.assertEqual(await self.consumer.tick(mixed, now=NOW + timedelta(seconds=1)), "unknown")
        self.assertEqual(calls, 2)

    async def test_stale_dismiss_cannot_hide_a_missing_episode(self) -> None:
        self.project(("example:a",))
        await self.tick()
        self.project((), NOW + timedelta(seconds=1))
        await self.tick(1)
        self.project((), NOW + timedelta(days=31))
        await self.consumer.tick(self.send, now=NOW + timedelta(days=31))
        self.assertFalse(self.consumer.dismiss(self.sent[0][1], OWNER, OWNER, 1))

    async def test_changed_seed_preserves_cursor_and_sends_nothing(self) -> None:
        self.project(())
        await self.tick()
        data = json.loads(self.journal.read_text())
        data["seed_binding"] = "a" * 64
        self.journal.write_text(json.dumps(data))
        before = self.cursor.read_bytes()
        self.assertEqual(await self.tick(1), "unavailable")
        self.assertEqual(self.cursor.read_bytes(), before)
        self.assertEqual(len(self.sent), 1)

    async def test_invalid_cursor_is_preserved_without_losing_delivery_fences(self) -> None:
        self.project(())
        await self.tick()
        for data in (b"{broken", b'{"schema_version":1,"schema_version":1}', b"[]"):
            self.cursor.write_bytes(data)
            self.assertEqual(await self.tick(1), "unavailable")
            self.assertEqual(self.cursor.read_bytes(), data)
        self.assertEqual(len(self.sent), 1)

    async def test_sustained_storm_keeps_one_deferred_summary(self) -> None:
        self.settings = NoticeSettings(
            self.journal, self.cursor, OWNER, interval=5, max_cards=1, window_seconds=3600
        )
        self.consumer = HermesIncidentNotices(self.settings)
        self.project(())
        await self.tick()
        for second in range(5, 1005, 5):
            self.project((f"example:{second}",), NOW + timedelta(seconds=second))
            self.assertEqual(await self.tick(second), "idle")
        self.assertEqual(len(self.sent), 1)
        pending = [r for r in self.saved()["deliveries"] if r["state"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertIn("Сводка", pending[0]["text"])
        await self.tick(3600)
        self.assertEqual(len(self.sent), 2)

    def test_settings_refuse_sidecar_collision(self) -> None:
        for cursor in (
            self.journal.with_suffix(".seed"),
            self.journal.with_suffix(".lock"),
            self.root / "incident-journal.cursor",
            self.root / "own.lock",
        ):
            with self.subTest(cursor=cursor), self.assertRaises(ValueError):
                NoticeSettings(self.journal, cursor, OWNER)

    async def test_cancelled_file_work_is_joined_and_keeps_lock_until_fenced(self) -> None:
        self.project(())
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        write = self.consumer.store.write
        calls = 0

        def delayed(value: dict) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise TimeoutError("fixture release")
            write(value)

        self.consumer.store.write = delayed
        task = asyncio.create_task(self.consumer.tick(self.send, now=NOW))
        await asyncio.wait_for(entered.wait(), 3)
        try:
            task.cancel()
            await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            self.assertEqual(
                await HermesIncidentNotices(self.settings).tick(self.send, now=NOW), "busy"
            )
        finally:
            release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.saved()["deliveries"][0]["state"], "attempted")
        self.consumer = HermesIncidentNotices(self.settings)
        await self.tick(1)
        self.assertEqual(self.sent, [])

    async def test_send_timeout_remains_unknown_without_retry(self) -> None:
        self.project(())

        async def blocked(text: str, token: str) -> int:
            await asyncio.Event().wait()
            return 1

        self.assertEqual(await self.consumer.tick(blocked, now=NOW), "unknown")
        self.assertEqual(self.saved()["deliveries"][0]["state"], "unknown")
        await self.tick(1)
        self.assertEqual(self.sent, [])

    async def test_failed_off_loop_read_is_reported_without_writing(self) -> None:
        with self.assertRaises(OSError):
            await file_call(mock.Mock(side_effect=OSError("fixture")))

    async def test_uncertain_cursor_saturation_preserves_all_evidence(self) -> None:
        self.project(())
        await self.tick()
        value = self.saved()
        template = value["deliveries"][0]
        value["deliveries"] = [
            {**template, "token": f"{n:032x}", "state": "unknown", "message_id": None}
            for n in range(128)
        ]
        self.consumer.store.write(value)
        before = self.cursor.read_bytes()
        self.project(("example:new",), NOW + timedelta(seconds=1))
        self.assertEqual(await self.tick(1), "unavailable")
        self.assertEqual(self.cursor.read_bytes(), before)
        self.assertEqual(len(self.sent), 1)

    async def test_storm_cannot_change_a_rejected_retrys_exact_notice(self) -> None:
        self.project(())

        async def rejected(text: str, token: str) -> int:
            raise NoticeRejected(20)

        await self.consumer.tick(rejected, now=NOW)
        original = dict(self.saved()["deliveries"][0])
        self.project(tuple(f"example:{n}" for n in range(30)), NOW + timedelta(seconds=1))
        await self.tick(1)
        self.assertEqual(self.saved()["deliveries"][0], original)
        await self.tick(20)
        self.assertEqual(self.sent[-1], (original["text"], original["token"]))

    async def test_unsafe_parent_refuses_before_creating_a_lock(self) -> None:
        self.project(())
        self.root.chmod(0o755)
        try:
            self.assertEqual(await self.tick(), "unavailable")
            self.assertFalse(self.cursor.with_suffix(".lock").exists())
            self.assertEqual(self.sent, [])
        finally:
            self.root.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
