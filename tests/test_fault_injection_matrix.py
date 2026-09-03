from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_codex_router.state import HubState
from tests.fault_matrix_support import FaultMatrixHarness, RecordingAdapter

ROOT = Path(__file__).resolve().parents[1]
ACTOR = ROOT / "tests" / "fault_matrix_actor.py"
TIMEOUT_SECONDS = 8.0


class ProcessBoundaryFaultInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.harness = FaultMatrixHarness(self.base)
        self.children: list[subprocess.Popen[str]] = []

    def tearDown(self) -> None:
        for child in self.children:
            self.terminate(child)
        self.tempdir.cleanup()

    def spawn(self, mode: str, *arguments: object) -> subprocess.Popen[str]:
        environment = dict(os.environ)
        paths = [str(ROOT / "src"), str(ROOT)]
        if "PYTHONPATH" in os.environ:
            paths.append(os.environ["PYTHONPATH"])
        environment["PYTHONPATH"] = os.pathsep.join(paths)
        child = subprocess.Popen(
            [sys.executable, str(ACTOR), mode, str(self.base), *(str(arg) for arg in arguments)],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.children.append(child)
        return child

    def wait_marker(self, child: subprocess.Popen[str], marker: Path) -> None:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if marker.exists():
                return
            returncode = child.poll()
            if returncode is not None:
                stdout, stderr = child.communicate()
                self.fail(
                    f"fault actor exited before {marker.name}: {returncode}\n{stdout}\n{stderr}"
                )
            time.sleep(0.01)
        self.fail(f"timed out waiting for fault marker: {marker.name}")

    def wait_exit(self, child: subprocess.Popen[str]) -> None:
        try:
            returncode = child.wait(timeout=TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            self.terminate(child)
            self.fail("fault actor did not exit within its bounded wait")
        stdout, stderr = child.communicate()
        if returncode != 0:
            self.fail(f"fault actor failed: {returncode}\n{stdout}\n{stderr}")

    def terminate(self, child: subprocess.Popen[str]) -> None:
        if child.poll() is not None:
            child.communicate()
            return
        child.terminate()
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=2)
        child.communicate()

    def admit(self, message_id: int, thread_id: int, mention: str, text: str) -> str:
        controller = self.harness.controller()
        try:
            self.assertTrue(
                controller.handle_update(
                    self.harness.update(message_id, thread_id, f"@{mention} {text}")
                )
            )
            return self.harness.one_job(thread_id).job_id
        finally:
            controller.state.close()

    def test_controller_killed_after_enqueue_redelivers_without_duplicate_job(self) -> None:
        enqueued = self.base / "controller-enqueued.marker"
        child = self.spawn(
            "controller-block-after-enqueue",
            201,
            801,
            "@example_opencode_bot durable request",
            enqueued,
        )
        self.wait_marker(child, enqueued)
        self.terminate(child)

        state = HubState.open(self.harness.config.state_path)
        try:
            self.assertIsNone(state.get_bot_offset("hub"))
            original = self.harness.one_job(801)
            self.assertEqual(original.status, "queued")
        finally:
            state.close()

        restarted = self.base / "controller-restarted.marker"
        replacement = self.spawn(
            "controller-once",
            "hub",
            "group",
            201,
            801,
            "@example_opencode_bot durable request",
            restarted,
        )
        self.wait_marker(replacement, restarted)
        self.wait_exit(replacement)

        state = HubState.open(self.harness.config.state_path)
        try:
            self.assertEqual(state.get_bot_offset("hub"), 202)
            redelivered = self.harness.one_job(801)
            self.assertEqual(redelivered.job_id, original.job_id)
        finally:
            state.close()

        adapter = RecordingAdapter("opencode")
        worker = self.harness.worker("opencode", adapter)
        try:
            self.assertTrue(worker.run_cycle())
            self.assertFalse(worker.run_cycle())
            self.assertEqual(len(adapter.calls), 1)
            self.assertIn("TELEGRAM INTERACTION CONTRACT v1", adapter.calls[0])
            self.assertTrue(adapter.calls[0].endswith("CURRENT USER TURN:\ndurable request"))
            self.assertEqual(worker.state.get_provider_job(original.job_id).status, "result_ready")
            self.assertEqual(
                worker.state.get_telegram_outbox_for_job(original.job_id).status,
                "pending",
            )
        finally:
            worker.close()

    def test_worker_process_loss_preserves_pre_and_post_execution_boundaries(self) -> None:
        pre_job_id = self.admit(202, 802, "example_opencode_bot", "recover pre-execution lease")
        pre_marker = self.base / "worker-leased.marker"
        pre_child = self.spawn("worker-block-after-lease", "opencode", pre_marker)
        self.wait_marker(pre_child, pre_marker)
        self.terminate(pre_child)

        state = HubState.open(self.harness.config.state_path)
        try:
            recovery = state.recover_stale_provider_jobs(
                agent_id="opencode",
                now=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            self.assertEqual(recovery.requeued_job_ids, (pre_job_id,))
        finally:
            state.close()

        recovered_adapter = RecordingAdapter("opencode")
        replacement = self.harness.worker("opencode", recovered_adapter)
        try:
            self.assertTrue(replacement.run_cycle())
            self.assertEqual(len(recovered_adapter.calls), 1)
            self.assertIn("TELEGRAM INTERACTION CONTRACT v1", recovered_adapter.calls[0])
            self.assertTrue(
                recovered_adapter.calls[0].endswith(
                    "CURRENT USER TURN:\nrecover pre-execution lease"
                )
            )
        finally:
            replacement.close()

        executing_job_id = self.admit(
            203, 803, "example_opencode_bot", "ambiguous provider invocation"
        )
        invocation = self.base / "provider-invocation.marker"
        executing_child = self.spawn("worker-block-in-adapter", "opencode", invocation)
        self.wait_marker(executing_child, invocation)
        self.terminate(executing_child)

        state = HubState.open(self.harness.config.state_path)
        try:
            recovery = state.recover_stale_provider_jobs(
                agent_id="opencode",
                now=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            self.assertEqual(recovery.indeterminate_job_ids, (executing_job_id,))
            self.assertEqual(state.get_provider_job(executing_job_id).status, "indeterminate")
        finally:
            state.close()

        forbidden_replay = RecordingAdapter("opencode")
        final_worker = self.harness.worker("opencode", forbidden_replay)
        try:
            self.assertFalse(final_worker.run_cycle())
            self.assertEqual(forbidden_replay.calls, [])
        finally:
            final_worker.close()

    def test_sender_killed_after_acceptance_retries_outbox_without_provider_replay(self) -> None:
        job_id = self.admit(204, 804, "example_opencode_bot", "prepare durable result")
        provider_invocation = self.base / "completed-provider.marker"
        worker = self.spawn("worker-once", "opencode", provider_invocation)
        self.wait_marker(worker, provider_invocation)
        self.wait_exit(worker)

        accepted = self.base / "telegram-accepted.marker"
        sender = self.spawn("sender-block-after-acceptance", accepted)
        self.wait_marker(sender, accepted)
        self.terminate(sender)

        state = HubState.open(self.harness.config.state_path)
        try:
            outbox = state.get_telegram_outbox_for_job(job_id)
            self.assertEqual(outbox.status, "sending")
            recovered = state.recover_stale_telegram_outbox(
                sender_agent_ids=("codex", "opencode", "antigravity"),
                now=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            self.assertEqual(recovered, (outbox.outbox_id,))
        finally:
            state.close()

        delivered = self.base / "telegram-retried.marker"
        retry = self.spawn("sender-once", delivered, 300)
        self.wait_marker(retry, delivered)
        self.wait_exit(retry)

        state = HubState.open(self.harness.config.state_path)
        try:
            self.assertEqual(state.get_provider_job(job_id).status, "completed")
            self.assertTrue(
                state.get_provider_result(job_id).visible_response.startswith("opencode")
            )
            self.assertEqual(
                provider_invocation.read_text(encoding="utf-8").splitlines(), ["invoked"]
            )
        finally:
            state.close()

    def test_hung_provider_process_does_not_block_peer_worker_or_controller_status(self) -> None:
        hung_job_id = self.admit(205, 805, "example_opencode_bot", "hung provider task")
        healthy_job_id = self.admit(206, 806, "example_antigravity_bot", "healthy provider task")
        hung_marker = self.base / "hung-provider.marker"
        hung = self.spawn("worker-block-in-adapter", "opencode", hung_marker)
        self.wait_marker(hung, hung_marker)

        healthy_marker = self.base / "healthy-provider.marker"
        healthy = self.spawn("worker-once", "antigravity", healthy_marker)
        self.wait_marker(healthy, healthy_marker)
        self.wait_exit(healthy)
        self.assertIsNone(hung.poll())

        status_marker = self.base / "controller-status.marker"
        status = self.spawn("controller-once", "hub", "group", 207, 805, "/status", status_marker)
        self.wait_marker(status, status_marker)
        self.wait_exit(status)
        self.assertIsNone(hung.poll())

        state = HubState.open(self.harness.config.state_path)
        try:
            self.assertEqual(state.get_provider_job(hung_job_id).status, "executing")
            self.assertEqual(state.get_provider_job(healthy_job_id).status, "result_ready")
            self.assertIn("No active agent session", status_marker.read_text(encoding="utf-8"))
        finally:
            state.close()
        self.terminate(hung)

    def test_real_polling_loops_keep_hub_and_direct_provider_ingress_distinct(self) -> None:
        state = HubState.open(self.harness.config.state_path)
        try:
            state.set_bot_offset("hub", 41)
            state.set_bot_offset("codex", 73)
        finally:
            state.close()
        hub_marker = self.base / "hub-poll.marker"
        hub = self.spawn(
            "controller-once",
            "hub",
            "group",
            208,
            808,
            "@example_opencode_bot Hub group request",
            hub_marker,
        )
        self.wait_marker(hub, hub_marker)
        self.wait_exit(hub)

        direct_marker = self.base / "codex-direct-poll.marker"
        direct = self.spawn(
            "controller-once",
            "codex",
            "direct",
            309,
            909,
            "group message ignored by direct provider",
            direct_marker,
        )
        self.wait_marker(direct, direct_marker)
        self.wait_exit(direct)

        state = HubState.open(self.harness.config.state_path)
        try:
            self.assertEqual(hub_marker.read_text(encoding="utf-8"), "offset:41")
            self.assertEqual(direct_marker.read_text(encoding="utf-8"), "offset:73")
            self.assertEqual(state.get_bot_offset("hub"), 209)
            self.assertEqual(state.get_bot_offset("codex"), 310)
            self.assertIsNotNone(state.find_topic(self.harness.chat_id, 808))
            self.assertIsNone(state.find_topic(self.harness.chat_id, 909))
            self.assertEqual(self.harness.one_job(808).agent_id, "opencode")
        finally:
            state.close()


if __name__ == "__main__":
    unittest.main()
