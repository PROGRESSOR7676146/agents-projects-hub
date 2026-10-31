"""Real owned subprocess + worker + SQLite; no native model or network."""

from __future__ import annotations

import sqlite3
import unittest
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from hermes_codex_router.claude_correction_state import ClaudeCorrectionState
from hermes_codex_router.claude_corrections import ClaudeCorrectionInput, ClaudeInputFrame
from hermes_codex_router.external_runtime import ExternalCliAdapter
from hermes_codex_router.root_blockers import persistent_root_blocker
from tests import test_claude_native_worker as fixtures
from tests.delivery_fixture import complete_final_delivery
from tests.test_external_runtime import fictional_claude_source


class ClaudeCorrectionWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.ClaudeNativeWorkerTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.fixture.config = replace(self.fixture.fixture.config, claude_stream_input=True)
        self.enterContext(
            patch.dict(
                "os.environ",
                {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                    "ANTHROPIC_AUTH_TOKEN": "example",
                },
                clear=True,
            )
        )

    def adapter(
        self,
        *,
        late: bool = False,
        missing: bool = False,
        extra: str = "",
        blocked: bool = False,
        accept_child: bool = True,
    ) -> ExternalCliAdapter:
        executable = self.fixture.root / "fictional-provider"
        source = fictional_claude_source(
            "import json,os,sys\n"
            "native=next(sys.argv[sys.argv.index(flag)+1] for flag in ('--session-id','--resume') if flag in sys.argv)\n"
            "def emit(value): print(json.dumps(value),flush=True)\n"
            "def result(text): emit(dict(type='result',subtype='success',is_error=False,session_id=native,result=text))\n"
            "if '--input-format' not in sys.argv:\n    result('Ordinary answer');sys.exit(0)\n"
            "first=json.loads(sys.stdin.readline());emit(dict(first,isReplay=True))\n"
            + (
                "result('Ordinary answer');assert sys.stdin.read()=='';sys.exit(0)\n"
                if not accept_child
                else ""
            )
            + "child=json.loads(sys.stdin.readline())\n"
            + ("result('First answer')\n" if late else "")
            + ("" if missing else "emit(dict(child,isReplay=True))\n")
            + ("import time;time.sleep(30)\n" if blocked else "")
            + ("" if missing else "result('Correction answer')\n")
            + ("sys.exit(0)\n" if missing else "assert sys.stdin.read()==''\n")
            + extra
        )
        # Add the streamed-input declarations to the fictional --help reply.
        source = source.replace(
            "if sys.argv[1:] == ['--help']:\n",
            "if sys.argv[1:] == ['--help']:\n    print('  --input-format <format>  Values: stream-json\\n  --replay-user-messages  Replay processed input')\n",
        )
        executable.write_text(source)
        executable.chmod(0o700)
        return ExternalCliAdapter("claude", executable=str(executable))

    def worker(self, adapter: ExternalCliAdapter):
        return self.fixture.worker(adapter)  # type: ignore[arg-type]

    def test_current_and_late_native_results_settle_exact_child_and_one_outbox(self) -> None:
        for late in (False, True):
            with self.subTest(late=late):
                self.setUp()
                parent, child, tail = [self.fixture.enqueue(i) for i in (1, 2, 3)]
                worker = self.worker(self.adapter(late=late))
                self.assertTrue(worker.run_cycle())
                state = worker.state
                self.assertEqual(state.get_provider_job(parent).status, "result_ready")
                self.assertEqual(state.get_provider_job(child).status, "completed")
                self.assertEqual(state.get_provider_job(tail).status, "queued")
                run = state._connection.execute(
                    "SELECT * FROM claude_input_runs WHERE parent_job_id=?", (parent,)
                ).fetchone()
                self.assertEqual(run["terminal_count"], 2 if late else 1)
                saved_child = ClaudeCorrectionState(state._connection).child(parent)
                assert saved_child is not None
                self.assertEqual(
                    saved_child["receipt_terminal"],
                    2 if late else 1,
                )
                self.assertIn(
                    "Correction answer", state.get_provider_result(parent).visible_response
                )
                self.assertEqual(
                    state._connection.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    state._connection.execute(
                        "SELECT COUNT(*) FROM provider_job_inputs"
                    ).fetchone()[0],
                    3,
                )
                self.assertFalse(worker.run_cycle())

    def test_missing_processed_receipt_retains_both_inputs_without_replay(self) -> None:
        parent, child = [self.fixture.enqueue(i) for i in (1, 2)]
        worker = self.worker(self.adapter(late=True, missing=True))
        self.assertTrue(worker.run_cycle())
        self.assertEqual(worker.state.get_provider_job(parent).status, "indeterminate")
        self.assertEqual(worker.state.get_provider_job(child).status, "indeterminate")
        self.assertIsNotNone(
            persistent_root_blocker(
                worker.state._connection, topic_id=worker.state.get_provider_job(parent).topic_id
            )
        )
        self.assertFalse(worker.run_cycle())

    def test_child_receipt_persistence_fault_terminates_process_and_preserves_uncertainty(
        self,
    ) -> None:
        parent, child = [self.fixture.enqueue(i) for i in (1, 2)]
        adapter = self.adapter()
        worker = self.worker(adapter)
        original = ClaudeCorrectionState.receipt

        def fail(store, parent_id, token, identifier, boundary):
            run = store.db.execute(
                "SELECT initial_input_uuid FROM claude_input_runs WHERE parent_job_id=?",
                (parent_id,),
            ).fetchone()
            if run[0] != identifier:
                raise RuntimeError("Example persistence fault")
            return original(store, parent_id, token, identifier, boundary)

        with patch.object(ClaudeCorrectionState, "receipt", new=fail):
            self.assertTrue(worker.run_cycle())
        self.assertIsNone(adapter._active_process)
        self.assertEqual(worker.state.get_provider_job(parent).status, "indeterminate")
        self.assertEqual(worker.state.get_provider_job(child).status, "indeterminate")

    def test_claim_is_durable_before_first_correction_byte(self) -> None:
        parent, child = [self.fixture.enqueue(i) for i in (1, 2)]
        worker = self.worker(self.adapter())
        original = ClaudeInputFrame.encode
        observed = []

        def encode(frame):
            row = ClaudeCorrectionState(worker.state._connection).child(parent)
            if row is not None and row["input_uuid"] == frame.uuid:
                observed.append(worker.state.get_provider_job(child).status)
            return original(frame)

        with patch.object(ClaudeInputFrame, "encode", new=encode):
            worker.run_cycle()
        self.assertEqual(observed, ["executing"])

    def test_default_keeps_correction_queued(self) -> None:
        self.fixture.fixture.config = replace(
            self.fixture.fixture.config, claude_stream_input=False
        )
        parent, child = [self.fixture.enqueue(i) for i in (1, 2)]
        worker = self.worker(self.adapter())
        worker.run_cycle()
        self.assertEqual(worker.state.get_provider_job(parent).status, "result_ready")
        self.assertEqual(worker.state.get_provider_job(child).status, "queued")

    def test_nonzero_exit_after_results_does_not_confirm_child(self) -> None:
        parent, child = [self.fixture.enqueue(i) for i in (1, 2)]
        worker = self.worker(self.adapter(extra="sys.exit(7)\n"))
        worker.run_cycle()
        self.assertEqual(worker.state.get_provider_job(parent).status, "indeterminate")
        self.assertEqual(worker.state.get_provider_job(child).status, "indeterminate")

    def test_unreserved_admission_fault_preserves_parent_and_fifo(self) -> None:
        for failure in ("root", "database"):
            with self.subTest(failure=failure):
                self.setUp()
                parent, child = [self.fixture.enqueue(i) for i in (1, 2)]
                worker = self.worker(self.adapter(accept_child=False))
                target = (
                    "hermes_codex_router.worker_claude_corrections.revalidate_worker_execution_root"
                    if failure == "root"
                    else "hermes_codex_router.claude_correction_state.ClaudeCorrectionState.claim"
                )
                with patch(
                    target, side_effect=sqlite3.OperationalError("Example admission failure")
                ) as failed:
                    worker.run_cycle()
                self.assertEqual(failed.call_count, 1)
                self.assertEqual(worker.state.get_provider_job(parent).status, "result_ready")
                saved = worker.state.get_provider_job(child)
                self.assertEqual((saved.status, saved.attempt_count), ("queued", 0))

    def test_mid_turn_model_selection_does_not_invalidate_original_result(self) -> None:
        parent = self.fixture.enqueue(1)
        worker = self.worker(self.adapter(accept_child=False))
        original = ClaudeCorrectionInput.observe

        def change(channel, event):
            retain = original(channel, event)
            if event.get("isReplay") is True:
                with worker.state._connection:
                    worker.state._connection.execute(
                        "UPDATE agent_sessions SET model='other-model',effort='low'"
                    )
            return retain

        with patch.object(ClaudeCorrectionInput, "observe", new=change):
            worker.run_cycle()
        self.assertEqual(worker.state.get_provider_job(parent).status, "result_ready")

    def test_admission_commit_ambiguity_cannot_fall_back_to_fifo(self) -> None:
        parent, child = [self.fixture.enqueue(i) for i in (1, 2)]
        worker = self.worker(self.adapter())
        transaction = worker.state._immediate_transaction
        injected = []

        @contextmanager
        def ambiguous_commit():
            with transaction():
                yield
            if not injected and ClaudeCorrectionState(worker.state._connection).child(parent):
                injected.append(True)
                raise RuntimeError("Example error after reservation committed")

        with patch.object(worker.state, "_immediate_transaction", new=ambiguous_commit):
            worker.run_cycle()
        self.assertEqual(injected, [True])
        self.assertEqual(worker.state.get_provider_job(parent).status, "indeterminate")
        self.assertEqual(worker.state.get_provider_job(child).status, "indeterminate")

    def test_stop_after_child_receipt_requires_established_session(self) -> None:
        for established in (False, True):
            with self.subTest(established=established):
                self.setUp()
                if established:
                    self.fixture.enqueue(1)
                    initial = self.fixture.worker(
                        fixtures.ObservingClaudeAdapter(self.fixture.path)
                    )
                    initial.config = replace(initial.config, claude_stream_input=False)
                    self.assertTrue(initial.run_cycle())
                    outbox = initial.state.lease_telegram_outbox("claude", "example-sender")
                    assert outbox and outbox.lease_token
                    complete_final_delivery(
                        initial.state, outbox.outbox_id, outbox.lease_token, telegram_message_id=201
                    )
                parent, child = [self.fixture.enqueue(i) for i in (2, 3)]
                adapter = self.adapter(blocked=True)
                worker = self.worker(adapter)
                original = ClaudeCorrectionInput.observe

                def stop(channel, event):
                    retain = original(channel, event)
                    saved = ClaudeCorrectionState(worker.state._connection).child(parent)
                    if (
                        saved is not None
                        and saved["input_uuid"] == event.get("uuid")
                        and event.get("isReplay") is True
                    ):
                        job = worker.state.get_provider_job(parent)
                        worker.state.request_emergency_stop(
                            topic_id=job.topic_id,
                            chat_id=job.chat_id,
                            message_id=99,
                            target_agent_id="claude",
                        )
                        adapter.interrupt()
                    return retain

                with patch.object(ClaudeCorrectionInput, "observe", new=stop):
                    self.assertTrue(worker.run_cycle())
                expected = "cancelled" if established else "indeterminate"
                outcome = worker.state.get_provider_job(parent)
                self.assertEqual(
                    outcome.status, expected, (outcome.error_code, outcome.error_detail)
                )
                self.assertEqual(worker.state.get_provider_job(child).status, expected)
                self.assertIsNone(adapter._active_process)

    def test_restart_recovers_saved_aggregate_without_another_invocation(self) -> None:
        parent, child = [self.fixture.enqueue(i) for i in (1, 2)]
        adapter = self.adapter(late=True)
        worker = self.worker(adapter)
        with patch.object(
            worker.state,
            "commit_provider_result",
            side_effect=RuntimeError("Example local commit fault"),
        ):
            self.assertTrue(worker.run_cycle())
        self.assertEqual(worker.state.get_provider_job(parent).status, "executing")
        self.assertEqual(worker.state.get_provider_job(child).status, "executing")
        with worker.state._connection:
            worker.state._connection.execute(
                "UPDATE provider_jobs SET lease_expires_at=? WHERE status='executing'",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
            )
        with patch.object(adapter, "run_turn", side_effect=AssertionError("no replay")):
            self.assertTrue(worker.run_cycle())
        self.assertEqual(worker.state.get_provider_job(parent).status, "result_ready")
        self.assertEqual(worker.state.get_provider_job(child).status, "completed")
        self.assertIn("First answer", worker.state.get_provider_result(parent).visible_response)
        self.assertIn(
            "Correction answer", worker.state.get_provider_result(parent).visible_response
        )


if __name__ == "__main__":
    unittest.main()
