from __future__ import annotations

import json
import queue
import subprocess
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from hermes_codex_router.codex_appserver import CodexAppServerClient, CodexTurnError, RpcError
from hermes_codex_router.codex_permissions import CodexPermissionPolicyDriftError
from hermes_codex_router.codex_rpc import RpcOutboundUnavailableError
from hermes_codex_router.codex_transports import StdioJsonLineTransport

NOTICE = "Hub transport notice: the response channel failed. Hub granted no permission."
THREAD = "example-thread"
TURN = "example-turn"


def notification(method, **params):
    return {"method": method, "params": {"threadId": THREAD, "turnId": TURN, **params}}


def completed(**changes):
    value = notification("turn/completed", turn={"id": TURN, "status": "completed"})
    value["params"].update(changes)
    return value


def visible():
    return notification(
        "item/completed",
        item={
            "id": "example-final",
            "type": "agentMessage",
            "phase": "final_answer",
            "text": "Final.",
        },
    )


class QueueReader:
    def __init__(self):
        self.frames = queue.Queue()

    def emit(self, *frames):
        for frame in frames:
            self.frames.put(json.dumps(frame) + "\n")

    def readline(self, size=-1):
        return self.frames.get(timeout=5)

    def close(self):
        pass


class StdioFailureDrainTests(unittest.TestCase):
    def accepted_client(self, *, failing_decline=False):
        reader = QueueReader()
        entered = threading.Event()
        release = threading.Event()
        writes = []

        class Writer:
            def write(self, line):
                frame = json.loads(line)
                if frame.get("method") == "turn/start":
                    writes.append(frame)
                    reader.emit({"id": frame["id"], "result": {"turn": {"id": TURN}}})
                elif failing_decline:
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("fictional writer was not released")
                    raise BrokenPipeError("fictional private diagnostic")
                else:
                    writes.append(frame)
                return len(line)

            def flush(self):
                pass

            def close(self):
                pass

        process = SimpleNamespace(stdin=Writer(), stdout=reader, poll=lambda: 0)
        transport = StdioJsonLineTransport(
            cast(subprocess.Popen[str], process), max_pending_frames=1
        )
        self.addCleanup(transport.close)
        self.addCleanup(reader.frames.put, "")
        self.addCleanup(release.set)
        client = CodexAppServerClient(transport, initialized=True, approval_policy="never")
        turn = client.start_turn(
            thread_id=THREAD, text="Input.", cwd=Path.cwd(), model="example", effort="high"
        )
        self.assertEqual(turn, TURN)
        return client, transport, reader, entered, release, writes

    def test_async_decline_failure_keeps_exact_final_telemetry_and_persisted_notice(self):
        client, transport, reader, entered, release, writes = self.accepted_client(
            failing_decline=True
        )
        saved, items, errors = [], [], []
        client.on_completed = saved.append
        client.on_visible_item = lambda *item: items.append(item)

        def tail():
            try:
                if not entered.wait(5):
                    raise AssertionError("decline never reached writer")
                release.set()
                transport._writer_thread.join(5)
                if transport._writer_thread.is_alive():
                    raise AssertionError("writer failure was not recorded")
                reader.emit(
                    notification(
                        "thread/tokenUsage/updated",
                        tokenUsage={"last": {"totalTokens": 25}, "modelContextWindow": 100},
                    ),
                    {
                        "method": "account/rateLimits/updated",
                        "params": {
                            "rateLimits": {
                                "primary": {"usedPercent": 40, "windowDurationMins": 10080}
                            }
                        },
                    },
                    visible(),
                    completed(),
                )
                reader.frames.put("")
            except Exception as error:
                errors.append(error)
                reader.frames.put("")

        publisher = threading.Thread(target=tail, daemon=True)
        publisher.start()
        self.addCleanup(publisher.join, 5)
        reader.emit(
            notification("item/fileChange/requestApproval", itemId="example-file")
            | {"id": "example-approval"}
        )
        result = client.wait_for_turn(TURN)
        self.assertEqual(errors, [])
        self.assertEqual(result.text, "Final.\n\n" + NOTICE)
        self.assertEqual(saved, [result])
        self.assertEqual(items, [("example-final", "Final.", "final_answer")])
        self.assertEqual((result.context_window, result.context_tokens_used), (100, 25))
        limits = next(iter(client._turn_rate_limits.values()))
        assert limits.primary is not None
        self.assertEqual(limits.primary.remaining_percent, 60)
        self.assertEqual([frame["method"] for frame in writes], ["turn/start"])

    def test_eof_before_buffered_approval_still_saves_exact_final(self):
        client, transport, reader, _, _, writes = self.accepted_client()
        # Put the tail in the client's already-retained notifications. EOF can
        # then be recorded before it handles the earlier approval.
        client.notifications.extend(
            [
                notification("item/fileChange/requestApproval") | {"id": "example-approval"},
                visible(),
                completed(),
            ]
        )
        reader.frames.put("")
        transport._reader_thread.join(5)
        self.assertFalse(transport._reader_thread.is_alive())
        saved = []
        client.on_completed = saved.append
        result = client.wait_for_turn(TURN)
        self.assertEqual(result.text, "Final.\n\n" + NOTICE)
        self.assertEqual(saved, [result])
        self.assertEqual(len(writes), 1)

    def test_healthy_eof_without_refused_reply_does_not_add_notice(self):
        client, transport, reader, _, _, _ = self.accepted_client()
        client.notifications.extend([visible(), completed()])
        reader.frames.put("")
        transport._reader_thread.join(5)
        self.assertEqual(client.wait_for_turn(TURN).text, "Final.")

    def test_generic_reply_failure_is_not_hidden_by_drain(self):
        client, transport, reader, _, _, _ = self.accepted_client()
        client.notifications.extend(
            [
                notification("item/fileChange/requestApproval") | {"id": "example-approval"},
                visible(),
                completed(),
            ]
        )
        with patch.object(transport, "send", side_effect=RpcError("fictional queue overflow")):
            with self.assertRaisesRegex((RpcError, CodexTurnError), "queue overflow"):
                client.wait_for_turn(TURN)

    def test_already_waiting_quiet_reader_observes_async_writer_fault_without_eof(self):
        client, transport, reader, entered, release, writes = self.accepted_client(
            failing_decline=True
        )
        reading, finished = threading.Event(), threading.Event()
        outcomes = []
        original_get = transport._inbound.get

        def get(*, timeout):
            reading.set()
            return original_get(timeout=timeout)

        def wait():
            try:
                outcomes.append(client.wait_for_turn(TURN))
            except Exception as error:
                outcomes.append(error)
            finally:
                finished.set()

        caller = threading.Thread(target=wait, daemon=True)
        self.addCleanup(caller.join, 5)
        self.addCleanup(reader.frames.put, "")
        with (
            patch.object(transport._inbound, "get", side_effect=get),
            patch(
                "hermes_codex_router.codex_transports.STDIO_FAILURE_POLL_SECONDS", 0.01, create=True
            ),
            patch("hermes_codex_router.codex_response_drain.RESPONSE_FAILURE_DRAIN_SECONDS", 0.05),
        ):
            caller.start()
            self.assertTrue(reading.wait(5))
            # Trigger the physical fault from outside receive; stdout remains
            # open and no further frame wakes its waiting consumer.
            transport.send({"id": "example-decline", "result": {"decision": "decline"}})
            self.assertTrue(entered.wait(5))
            release.set()
            transport._writer_thread.join(5)
            self.assertTrue(finished.wait(2), "quiet stdout hid the asynchronous writer fault")
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], CodexTurnError)
        self.assertIn("drain deadline", str(outcomes[0]))
        self.assertEqual(outcomes[0].partial_text, NOTICE)
        self.assertEqual([frame["method"] for frame in writes], ["turn/start"])


class DrainTransport:
    def __init__(self, frames, *, fault=True, receive_error=None):
        self.frames = iter(frames)
        self.writer_failure = RpcOutboundUnavailableError() if fault else None
        self.sent = []
        self.timeouts = []
        self.receive_error = receive_error

    def send(self, message):
        self.sent.append(message)
        if "result" in message or "error" in message:
            raise RpcOutboundUnavailableError()

    def receive(self, *, timeout=None):
        self.timeouts.append(timeout)
        try:
            return next(self.frames)
        except StopIteration:
            if self.receive_error:
                raise self.receive_error
            raise EOFError("fictional end") from None

    def close(self):
        pass


class DrainPolicyTests(unittest.TestCase):
    def client(self, transport):
        client = CodexAppServerClient(transport, initialized=True, approval_policy="never")
        client._activity_thread_id, client._activity_turn_id = THREAD, TURN
        return client

    def test_failed_channel_requires_explicit_exact_completion_proof(self):
        for params in (
            {"threadId": None},
            {"threadId": "example-foreign"},
            {"turn": {"id": TURN}},
            {"turn": {"id": "example-foreign", "status": "completed"}},
            {"turn": {"id": TURN, "status": "inProgress"}},
        ):
            with self.subTest(params=params):
                client = self.client(DrainTransport([visible(), completed(**params)]))
                saved = []
                client.on_completed = saved.append
                with self.assertRaises(CodexTurnError) as raised:
                    client.wait_for_turn(TURN)
                self.assertEqual(raised.exception.partial_text, "Final.\n\n" + NOTICE)
                self.assertEqual(saved, [])

    def test_foreign_flood_does_not_renew_failure_drain_or_start_again(self):
        transport = DrainTransport([notification("example/foreign") for _ in range(100)])
        client = self.client(transport)
        ticks = iter(range(1000))
        with patch(
            "hermes_codex_router.codex_appserver.time",
            SimpleNamespace(monotonic=lambda: next(ticks)),
        ):
            with self.assertRaisesRegex(CodexTurnError, "drain deadline") as raised:
                client.wait_for_turn(TURN)
        self.assertEqual(raised.exception.partial_text, NOTICE)
        self.assertLess(len(transport.timeouts), 21)
        self.assertTrue(all(0 < timeout <= 20 for timeout in transport.timeouts))
        self.assertFalse(transport.sent)

    def test_request_clamps_current_deadline_and_rejects_late_response(self):
        transport = DrainTransport(
            [
                notification("example/foreign"),
                {"id": 1, "result": {"turn": {"id": TURN}}},
            ]
        )
        clock = SimpleNamespace(now=0.0)
        original_receive = transport.receive

        def receive(*, timeout=None):
            frame = original_receive(timeout=timeout)
            clock.now += 12.0
            return frame

        client = self.client(transport)
        with (
            patch.object(transport, "receive", side_effect=receive),
            patch(
                "hermes_codex_router.codex_appserver.time",
                SimpleNamespace(monotonic=lambda: clock.now),
            ),
        ):
            with self.assertRaisesRegex(RpcError, "drain deadline"):
                client._request("example/read", {}, deadline=300)
        self.assertEqual(len(transport.sent), 1)
        self.assertTrue(all(0 < timeout <= 20 for timeout in transport.timeouts))

    def test_callback_failure_remains_primary_and_retains_visible_text_and_notice(self):
        for name in ("on_visible_item", "on_completed", "on_activity"):
            with self.subTest(callback=name):
                client = self.client(DrainTransport([visible(), completed()]))

                def fail(*args):
                    raise ValueError("fictional storage failed")

                setattr(client, name, fail)
                with self.assertRaisesRegex(CodexTurnError, "storage failed") as raised:
                    client.wait_for_turn(TURN)
                self.assertTrue(raised.exception.partial_text.endswith(NOTICE))
                if name != "on_activity":
                    self.assertTrue(raised.exception.partial_text.startswith("Final."))

    def test_partial_after_eof_keeps_notice_and_has_no_completion_checkpoint(self):
        client = self.client(DrainTransport([visible()]))
        saved = []
        client.on_completed = saved.append
        with self.assertRaises(CodexTurnError) as raised:
            client.wait_for_turn(TURN)
        self.assertEqual(raised.exception.partial_text, "Final.\n\n" + NOTICE)
        self.assertEqual(saved, [])

    def test_failed_channel_requires_the_previously_accepted_turn_identity(self):
        for accepted in (None, "example-other-turn", ""):
            with self.subTest(accepted=accepted):
                client = self.client(DrainTransport([visible(), completed()]))
                client._activity_turn_id = accepted
                saved = []
                client.on_completed = saved.append
                with self.assertRaises(CodexTurnError):
                    client.wait_for_turn(TURN)
                self.assertEqual(saved, [])

    def test_deadline_at_completion_boundary_retains_visible_partial(self):
        client = self.client(DrainTransport([visible(), completed()]))
        clock = SimpleNamespace(now=0.0)
        saved = []
        client.on_completed = saved.append

        observe = client._observe_activity

        def activity(message):
            observe(message)
            if message.get("method") == "turn/completed":
                clock.now = 25.0

        with (
            patch.object(client, "_observe_activity", side_effect=activity),
            patch(
                "hermes_codex_router.codex_appserver.time",
                SimpleNamespace(monotonic=lambda: clock.now),
            ),
        ):
            with self.assertRaisesRegex(CodexTurnError, "drain deadline") as raised:
                client.wait_for_turn(TURN)
        self.assertEqual(raised.exception.partial_text, "Final.\n\n" + NOTICE)
        self.assertEqual(saved, [])

    def test_permission_drift_remains_primary_after_channel_failure(self):
        client = self.client(DrainTransport([]))
        client._permission_drifted = True
        with self.assertRaises(CodexTurnError) as raised:
            client.wait_for_turn(TURN)
        self.assertEqual(
            raised.exception.failure_reason,
            CodexTurnError(CodexPermissionPolicyDriftError()).failure_reason,
        )
        self.assertEqual(raised.exception.partial_text, NOTICE)

    def test_refused_approval_reply_before_start_response_keeps_buffered_exact_completion(self):
        transport = DrainTransport(
            [
                notification("item/fileChange/requestApproval") | {"id": "example-approval"},
                visible(),
                completed(),
                {"id": 1, "result": {"turn": {"id": TURN}}},
            ],
            fault=False,
        )
        client = CodexAppServerClient(transport, initialized=True, approval_policy="never")
        turn = client.start_turn(
            thread_id=THREAD, text="Input.", cwd=Path.cwd(), model="example", effort="high"
        )
        saved = []
        client.on_completed = saved.append
        result = client.wait_for_turn(turn)
        self.assertEqual(saved, [result])
        self.assertEqual(result.text, "Final.\n\n" + NOTICE)
        self.assertEqual([frame.get("method") for frame in transport.sent], ["turn/start", None])
        self.assertEqual(transport.sent[1]["result"], {"decision": "decline"})
