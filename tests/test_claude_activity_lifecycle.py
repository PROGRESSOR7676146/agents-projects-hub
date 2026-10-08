"""Actual worker and sender failures cannot turn optional visibility into authority."""

from __future__ import annotations

import sqlite3
import sys
import unittest
from dataclasses import replace
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.claude_activity import ClaudeActivityState
from hermes_codex_router.external_runtime import ExternalCliAdapter
from hermes_codex_router.hub_config import HubTelegramBot
from hermes_codex_router.outbox_sender import TelegramOutboxSender
from tests import test_claude_native_worker as worker_fixtures
from tests import test_outbox_sender as sender_fixtures
from tests.test_claude_cli_capabilities import HELP


class ClaudeActivityLifecycleTests(unittest.TestCase):
    def owned_worker(self, failure: str | None = None) -> None:
        fixture = worker_fixtures.ClaudeNativeWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.fixture.config = replace(
            fixture.fixture.config,
            hub_bot=HubTelegramBot("example_hub_bot", fixture.root / "unused-token"),
        )
        executable = fixture.root / "fictional-claude"
        executable.write_text(
            f"#!{sys.executable}\nimport sys,json\n"
            f"if sys.argv[1:] == ['--help']:\n    print({HELP!r})\n    sys.exit(0)\n"
            "native=sys.argv[sys.argv.index('--session-id')+1]\n"
            "print(json.dumps({'type':'result','subtype':'success','is_error':False,"
            "'session_id':native,'result':'Fictional final answer'}))\n",
            encoding="utf-8",
        )
        executable.chmod(0o700)
        adapter = ExternalCliAdapter("claude", executable=str(executable))
        worker = fixture.worker(cast(Any, adapter))
        job_id = fixture.enqueue(1)
        processes = []
        observed_rows = []
        original_open = ClaudeActivityState.open_process_observation

        def observe(observer, *args, **kwargs):
            process = adapter._active_process
            assert process is not None
            processes.append(process)
            if failure == "open":
                raise RuntimeError("fictional optional failure")
            result = original_open(observer, *args, **kwargs)
            with sqlite3.connect(fixture.path) as independent:
                observed_rows.extend(
                    independent.execute(
                        "SELECT job_id,native_session_id,retired_at FROM claude_activity_observations"
                    ).fetchall()
                )
            return result

        with (
            patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                    "ANTHROPIC_AUTH_TOKEN": "example",
                },
                clear=True,
            ),
            patch.object(ClaudeActivityState, "open_process_observation", observe),
        ):
            if failure == "retire":
                with patch.object(
                    ClaudeActivityState, "retire", side_effect=RuntimeError("fictional")
                ):
                    self.assertTrue(worker.run_cycle())
            else:
                self.assertTrue(worker.run_cycle())
        self.assertEqual(len(processes), 1)
        self.assertEqual(processes[0].returncode, 0)
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)
        self.assertIsNone(adapter._active_process)
        self.assertEqual(worker.state.get_provider_job(job_id).status, "result_ready")
        self.assertEqual(
            worker.state.get_provider_result(job_id).visible_response, "Fictional final answer"
        )
        if failure != "open":
            self.assertEqual(observed_rows, [(job_id, adapter_native(worker, job_id), None)])
            retired = worker.state._connection.execute(
                "SELECT retired_at FROM claude_activity_observations WHERE job_id=?", (job_id,)
            ).fetchone()[0]
            self.assertEqual(retired is None, failure == "retire")
        else:
            self.assertEqual(observed_rows, [])

    def test_worker_callback_observes_actual_owned_process_and_committed_row(self) -> None:
        self.owned_worker()

    def test_observer_open_failure_preserves_result_and_owned_cleanup(self) -> None:
        self.owned_worker("open")

    def test_observer_retire_failure_preserves_result_and_owned_cleanup(self) -> None:
        self.owned_worker("retire")

    def test_sender_observer_failure_cannot_block_ready_final_delivery(self) -> None:
        fixture = sender_fixtures.TelegramOutboxSenderTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        job_id = fixture.ready_outbox("opencode", 501)
        bots = {"opencode": sender_fixtures.Bot(), "antigravity": sender_fixtures.Bot()}
        sender = TelegramOutboxSender(fixture.config, telegram_bots=cast(Any, bots))
        self.addCleanup(sender.close)
        with (
            patch.object(
                sender.claude_activity,
                "evaluate",
                side_effect=RuntimeError("fictional private detail"),
            ),
            patch("hermes_codex_router.outbox_sender.survived") as diagnostic,
        ):
            for _ in range(3):
                sender.run_cycle()
        self.assertEqual(len(bots["opencode"].sent), 1)
        self.assertEqual(sender.state.get_telegram_outbox_for_job(job_id).status, "delivered")
        self.assertTrue(
            all(
                call.args[0] == "outbox_sender.claude_activity"
                for call in diagnostic.call_args_list
            )
        )
        self.assertNotIn("private", bots["opencode"].sent[0][2])


def adapter_native(worker, job_id: str) -> str:
    checkpoint = worker.state._connection.execute(
        "SELECT provider_thread_id FROM provider_execution_checkpoints WHERE job_id=?", (job_id,)
    ).fetchone()
    return checkpoint[0]
