"""Fallback sends conservatively without erasing new-version receipt evidence."""

from __future__ import annotations

import unittest
from contextlib import closing

from hermes_codex_router.state import HubState
from tests import test_outbox_sender as fixtures


class Schema54RollbackTests(unittest.TestCase):
    def test_old_sender_preserves_attempt_receipt_and_conservative_purpose(self) -> None:
        fixture = fixtures.TelegramOutboxSenderTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        job = fixture.ready_outbox("opencode", 81)
        with closing(HubState.open_existing(fixture.config.state_path)) as state:
            db = state._connection
            outbox = state.get_telegram_outbox_for_job(job)
            with db:
                db.execute(
                    "UPDATE telegram_outbox SET status='unknown',send_started_at='example-start'"
                )
                db.execute(
                    """INSERT INTO telegram_send_attempts
                       (attempt_id,target_kind,target_id,part_index,binding_hash,started_at)
                       VALUES ('example-retained','final',?,1,?,'example-start')""",
                    (outbox.outbox_id, "a" * 64),
                )
                db.execute(
                    "INSERT INTO telegram_send_receipts VALUES ('example-retained',71,'example-receipt')"
                )
            before = {
                table: db.execute(f"SELECT * FROM {table}").fetchall()
                for table in ("telegram_send_attempts", "telegram_send_receipts")
            }
            sender = fixture.sender(opencode=fixtures.Bot(), antigravity=fixtures.Bot())
            self.addCleanup(sender.close)
            self.assertFalse(sender._deliver_one("opencode"))
            self.assertEqual(state.get_telegram_outbox_for_job(job).status, "unknown")
            self.assertEqual(state.get_provider_job(job).status, "result_ready")
            for table, rows in before.items():
                self.assertEqual(db.execute(f"SELECT * FROM {table}").fetchall(), rows)
            self.assertEqual(
                db.execute("SELECT purpose FROM telegram_outbox").fetchone()[0], "legacy"
            )
            self.assertEqual(db.execute("PRAGMA foreign_key_check").fetchall(), [])
