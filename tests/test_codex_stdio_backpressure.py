from __future__ import annotations

import io
import json
import queue
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

from hermes_codex_router.codex_appserver import CodexAppServerClient, RpcError
from hermes_codex_router.codex_inbox import BoundedInbox
from hermes_codex_router.codex_rpc import RpcOutboundUnavailableError
from hermes_codex_router.codex_transports import StdioJsonLineTransport


class ScriptedReader:
    def __init__(self, text, *, error=None, wait_at_end=False):
        self.text = io.StringIO(text)
        self.error = error
        self.wait_at_end = wait_at_end
        self.at_end = threading.Event()
        self.release = threading.Event()
        self.limits = []
        self.closed = False

    def readline(self, size=-1):
        self.limits.append(size)
        value = self.text.readline(size)
        if value:
            return value
        self.at_end.set()
        if self.wait_at_end and not self.release.wait(5):
            raise AssertionError("fictional reader was not released")
        if self.error is not None:
            raise self.error
        return ""

    def close(self):
        self.closed = True


class ControlledWriter:
    def __init__(self, *, error=None, close_error=None):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.writes = []
        self.error = error
        self.close_error = close_error
        self.closed = False

    def write(self, value):
        self.entered.set()
        if not self.release.wait(5):
            raise AssertionError("fictional writer was not released")
        if self.error is not None:
            raise self.error
        self.writes.append(value)
        return len(value)

    def flush(self):
        pass

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class StdioBackpressureTests(unittest.TestCase):
    def transport(self, frames=(), *, reader=None, frame_bytes=4 * 1024 * 1024, process=None):
        full = threading.Event()

        class ObservedInbox(BoundedInbox):
            def put(self, value, size):
                if self.pending_frames == 1:
                    full.set()
                return super().put(value, size)

        if reader is None:
            reader = ScriptedReader("".join(json.dumps(frame) + "\n" for frame in frames))
        if process is None:
            process = SimpleNamespace(stdin=io.StringIO(), stdout=reader, poll=lambda: 0)
        with patch("hermes_codex_router.codex_transports.BoundedInbox", ObservedInbox):
            transport = StdioJsonLineTransport(
                cast(subprocess.Popen[str], process),
                max_pending_frames=1,
                max_frame_bytes=frame_bytes,
            )
        self.addCleanup(transport.close)
        self.addCleanup(reader.release.set)
        return transport, full, reader

    def test_full_producer_preserves_every_frame_before_eof(self):
        frames = [{"id": number} for number in range(1100)]
        transport, full, _ = self.transport(frames)
        self.assertTrue(full.wait(1))
        self.assertEqual(transport._inbound.pending_frames, 1)
        transport.send({"method": "example-control"})
        self.assertEqual([transport.receive(timeout=1) for _ in frames], frames)
        with self.assertRaises(EOFError):
            transport.receive(timeout=1)
        transport._reader_thread.join(1)
        self.assertFalse(transport._reader_thread.is_alive())

    def test_large_rpc_and_full_stdout_cannot_deadlock_each_other(self):
        script = """
import json, sys
for _ in range(1100):
    print(json.dumps({'method':'item/agentMessage/delta','params':{'threadId':'example-other','turnId':'example-old','delta':'x'*1024}}), flush=True)
request = json.loads(sys.stdin.readline())
assert len(request['params']['input'][0]['text']) == 200000
print(json.dumps({'id':request['id'],'result':{'turn':{'id':'example-turn'}}}), flush=True)
"""
        process = subprocess.Popen(
            [sys.executable, "-I", "-u", "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        transport, full, _ = self.transport(process=process)
        finished = threading.Event()
        outcomes = []
        client = CodexAppServerClient(transport, initialized=True)

        def request():
            try:
                outcomes.append(
                    client.start_turn(
                        thread_id="example-thread",
                        text="x" * 200000,
                        cwd=Path.cwd(),
                        model="example",
                        effort="high",
                    )
                )
            except Exception as error:
                outcomes.append(error)
            finally:
                finished.set()

        caller = threading.Thread(target=request, daemon=True)
        try:
            self.assertTrue(full.wait(1))
            caller.start()
            self.assertTrue(finished.wait(2), "full stdout and synchronous stdin formed a cycle")
            self.assertEqual(outcomes, ["example-turn"])
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            transport.close()
            if caller.ident is not None:
                caller.join(1)

    def test_owned_subprocess_foreign_flood_keeps_approval_final_activity_and_telemetry(self):
        # A fixed fictional JSONL peer, never a provider executable or endpoint.
        script = """
import json, sys
def emit(value):
    print(json.dumps(value), flush=True)
for _ in range(1200):
    emit({'method':'item/agentMessage/delta','params':{'threadId':'example-other','turnId':'example-old'}})
request = json.loads(sys.stdin.readline())
assert request['method'] == 'turn/start'
emit({'id':'example-approval','method':'item/fileChange/requestApproval','params':{'threadId':'example-thread','turnId':'example-turn','itemId':'example-change'}})
decline = json.loads(sys.stdin.readline())
assert decline == {'id':'example-approval','result':{'decision':'decline'}}
emit({'method':'serverRequest/resolved','params':{'threadId':'example-thread','requestId':'example-approval'}})
params = {'threadId':'example-thread','turnId':'example-turn'}
emit({'method':'thread/tokenUsage/updated','params':dict(params,tokenUsage={'last':{'totalTokens':25},'modelContextWindow':100})})
emit({'method':'account/rateLimits/updated','params':{'rateLimits':{'primary':{'usedPercent':40,'windowDurationMins':10080}}}})
emit({'method':'item/completed','params':dict(params,item={'id':'example-final','type':'agentMessage','phase':'final_answer','text':'Example final.'})})
emit({'method':'turn/completed','params':dict(params,turn={'id':'example-turn','status':'completed'})})
emit({'id':request['id'],'result':{'turn':{'id':'example-turn'}}})
for _ in range(2):
    request = json.loads(sys.stdin.readline())
    assert request['method'] == 'account/rateLimits/read'
    emit({'id':request['id'],'result':{'rateLimits':{'primary':None,'secondary':None}}})
"""
        process = subprocess.Popen(
            [sys.executable, "-I", "-u", "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        transport, full, _ = self.transport(process=process)
        self.assertTrue(full.wait(1))
        events = []
        client = CodexAppServerClient(transport, initialized=True, approval_policy="never")
        client.on_activity = events.append
        turn = client.start_turn(
            thread_id="example-thread",
            text="Example input",
            cwd=Path.cwd(),
            model="example-offline",
            effort="high",
        )
        result = client.wait_for_turn(turn)
        self.assertEqual((turn, result.text), ("example-turn", "Example final."))
        self.assertEqual((result.context_window, result.context_tokens_used), (100, 25))
        limits = client.read_rate_limits()
        self.assertIsNotNone(limits.primary)
        assert limits.primary is not None
        self.assertEqual(
            (limits.primary.remaining_percent, limits.primary.duration_minutes), (60, 10080)
        )
        self.assertIsNone(client.read_rate_limits().primary)
        self.assertEqual(process.wait(timeout=2), 0)
        # A locally declined approval is not advertised as a human wait.
        self.assertEqual([event.kind for event in events], ["visible_message_completed"])

    def blocked_writer_transport(self, *, error=None, close_error=None):
        reader = ScriptedReader('{"id":"example-accepted"}\n', wait_at_end=True)
        writer = ControlledWriter(error=error, close_error=close_error)

        def terminate():
            writer.release.set()
            reader.release.set()

        process = SimpleNamespace(
            stdin=writer,
            stdout=reader,
            poll=lambda: None,
            terminate=terminate,
            wait=lambda timeout: 0,
        )
        transport, _, _ = self.transport(reader=reader, process=process)
        self.addCleanup(writer.release.set)
        transport.send({"id": "example-inflight"})
        self.assertTrue(writer.entered.wait(1))
        return transport, writer, reader

    def test_outbound_count_frame_and_aggregate_byte_bounds_refuse_without_extra_write(self):
        for by_bytes in (False, True):
            with self.subTest(by_bytes=by_bytes):
                transport, writer, _ = self.blocked_writer_transport()
                message = (
                    {"text": "x" * (3 * 1024 * 1024)} if by_bytes else {"id": "example-queued"}
                )
                count = 2 if by_bytes else 16
                for _ in range(count):
                    transport.send(message)
                with self.assertRaisesRegex(RpcError, "outbound buffer"):
                    transport.send(message)
                with self.assertRaisesRegex(RpcError, "outbound frame"):
                    transport.send({"text": "x" * (4 * 1024 * 1024)})
                self.assertEqual(transport._outbound.pending_frames, count)
                self.assertLessEqual(transport._outbound.pending_bytes, 8 * 1024 * 1024)
                self.assertFalse(writer.writes)
                transport.close()
                self.assertEqual(
                    [json.loads(line)["id"] for line in writer.writes], ["example-inflight"]
                )

    def test_close_stops_queued_sends_and_cleans_both_pipes_after_broken_pipe(self):
        transport, writer, reader = self.blocked_writer_transport(
            close_error=BrokenPipeError("fictional closed peer")
        )
        for identifier in range(3):
            transport.send({"id": identifier})
        transport.close()
        self.assertFalse(transport._writer_thread.is_alive())
        self.assertFalse(transport._reader_thread.is_alive())
        self.assertEqual(len(writer.writes), 1)
        self.assertTrue(writer.closed)
        self.assertTrue(reader.closed)
        with self.assertRaisesRegex(RpcError, "closed"):
            transport.send({"id": "example-after-close"})

    def test_close_terminates_owned_peer_that_never_reads_stdin(self):
        script = """
import threading
print('{"id":"example-ready"}', flush=True)
print('{"id":"example-wait"}', flush=True)
threading.Event().wait()
"""
        process = subprocess.Popen(
            [sys.executable, "-I", "-u", "-c", script],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        )
        transport, full, _ = self.transport(process=process)
        self.assertTrue(full.wait(1))
        entered_write = threading.Event()
        original_write = transport._writer.write

        def write(value):
            entered_write.set()
            return original_write(value)

        with patch.object(transport._writer, "write", side_effect=write):
            transport.send({"text": "x" * (2 * 1024 * 1024)})
            self.assertTrue(entered_write.wait(1))
            transport.close()
        self.assertIsNotNone(process.poll())
        self.assertFalse(transport._writer_thread.is_alive())
        self.assertFalse(transport._reader_thread.is_alive())
        self.assertTrue(transport._writer.closed)
        self.assertTrue(transport._reader.closed)

    def test_writer_failure_drains_accepted_inbound_before_original_cause(self):
        error = OSError("fictional writer failed")
        transport, writer, reader = self.blocked_writer_transport(error=error)
        writer.release.set()
        transport._writer_thread.join(1)
        self.assertFalse(transport._writer_thread.is_alive())
        self.assertEqual(transport.receive(timeout=1), {"id": "example-accepted"})
        reader.release.set()
        transport._reader_thread.join(5)
        with self.assertRaises(OSError) as raised:
            transport.receive(timeout=1)
        self.assertIs(raised.exception, error)
        with self.assertRaises(RpcOutboundUnavailableError) as refused:
            transport.send({"id": "example-refused"})
        self.assertIs(refused.exception.__cause__, error)

    def test_writer_failure_keeps_unread_pipe_tail_in_order_under_backpressure(self):
        frames = [
            {"id": "example-approval", "method": "item/fileChange/requestApproval"},
            {"method": "thread/tokenUsage/updated"},
            {"method": "account/rateLimits/updated"},
            {"method": "item/completed"},
            {"method": "turn/completed"},
        ]
        reader = ScriptedReader("".join(json.dumps(frame) + "\n" for frame in frames))
        error = OSError("fictional writer failure")
        writer = ControlledWriter(error=error)
        process = SimpleNamespace(stdin=writer, stdout=reader, poll=lambda: 0)
        transport, full, _ = self.transport(reader=reader, process=process)
        self.addCleanup(writer.release.set)
        self.assertTrue(full.wait(5))
        transport.send({"method": "example-control"})
        self.assertTrue(writer.entered.wait(5))
        transport.send({"method": "example-never-written"})
        writer.release.set()
        transport._writer_thread.join(5)
        self.assertFalse(transport._writer_thread.is_alive())
        self.assertEqual([transport.receive(timeout=5) for _ in frames], frames)
        with self.assertRaises(OSError) as raised:
            transport.receive(timeout=0)
        self.assertIs(raised.exception, error)
        self.assertFalse(writer.writes)

    def test_writer_fault_before_next_read_allows_delayed_tail_after_receive_timeout(self):
        class DelayedReader(ScriptedReader):
            def readline(self, size=-1):
                if self.text.tell() == len(self.text.getvalue()):
                    self.at_end.set()
                    if not self.release.wait(5):
                        raise AssertionError("fictional reader was not released")
                return self.text.readline(size)

        reader = DelayedReader('{"id":"example-accepted"}\n')
        error = OSError("fictional writer failure")
        writer = ControlledWriter(error=error)
        process = SimpleNamespace(stdin=writer, stdout=reader, poll=lambda: 0)
        transport, _, _ = self.transport(reader=reader, process=process)
        self.addCleanup(writer.release.set)
        self.assertTrue(reader.at_end.wait(5))
        transport.send({"method": "example-control"})
        self.assertTrue(writer.entered.wait(5))
        writer.release.set()
        transport._writer_thread.join(5)
        self.assertEqual(transport.receive(timeout=0), {"id": "example-accepted"})
        with self.assertRaisesRegex(RpcError, "timed out"):
            transport.receive(timeout=0)
        reader.text = io.StringIO('{"id":"example-delayed-final"}\n')
        reader.wait_at_end = False
        reader.release.set()
        self.assertEqual(transport.receive(timeout=5), {"id": "example-delayed-final"})
        with self.assertRaises(OSError) as raised:
            transport.receive(timeout=5)
        self.assertIs(raised.exception, error)

    def test_reader_failure_seals_outbound_without_losing_accepted_inbound(self):
        transport, writer, reader = self.blocked_writer_transport()
        transport.send({"id": "example-never-started"})
        error = OSError("fictional reader failed")
        reader.error = error
        reader.release.set()
        transport._reader_thread.join(1)
        self.assertFalse(transport._reader_thread.is_alive())
        with self.assertRaises(RpcOutboundUnavailableError) as raised:
            transport.send({"id": "example-refused"})
        self.assertIs(raised.exception.__cause__, error)
        writer.release.set()
        transport._writer_thread.join(1)
        self.assertFalse(transport._writer_thread.is_alive())
        self.assertEqual([json.loads(line)["id"] for line in writer.writes], ["example-inflight"])
        self.assertEqual(transport.receive(timeout=0), {"id": "example-accepted"})
        with self.assertRaises(OSError) as raised:
            transport.receive(timeout=0)
        self.assertIs(raised.exception, error)

    def test_reader_and_writer_faults_share_one_first_cause_in_each_order_and_race(self):
        for first in ("writer", "reader", "race"):
            with self.subTest(first=first):
                write_error, read_error = OSError("fictional write"), OSError("fictional read")
                transport, writer, reader = self.blocked_writer_transport(error=write_error)
                reader.error = read_error
                if first == "race":
                    barrier = threading.Barrier(3)
                    finish = transport._finish

                    def raced(error, **kwargs):
                        if error in (write_error, read_error):
                            barrier.wait(5)
                        return finish(error, **kwargs)

                    with patch.object(transport, "_finish", side_effect=raced):
                        writer.release.set()
                        reader.release.set()
                        barrier.wait(5)
                        transport._writer_thread.join(5)
                        transport._reader_thread.join(5)
                else:
                    if first == "writer":
                        writer.release.set()
                        transport._writer_thread.join(5)
                        reader.release.set()
                    else:
                        reader.release.set()
                        transport._reader_thread.join(5)
                        writer.release.set()
                    transport._reader_thread.join(5)
                    transport._writer_thread.join(5)
                self.assertFalse(transport._reader_thread.is_alive())
                self.assertFalse(transport._writer_thread.is_alive())
                cause = transport._inbound.terminal
                self.assertIs(cause, transport._outbound.terminal)
                self.assertIs(cause, transport._first_error)
                if first != "race":
                    self.assertIs(cause, write_error if first == "writer" else read_error)
                else:
                    self.assertIn(cause, (write_error, read_error))
                self.assertEqual(transport.receive(timeout=0), {"id": "example-accepted"})
                with self.assertRaises(OSError) as raised:
                    transport.receive(timeout=0)
                self.assertIs(raised.exception, cause)
                with self.assertRaises(RpcOutboundUnavailableError) as refused:
                    transport.send({"id": "example-refused"})
                self.assertIs(refused.exception.__cause__, cause)
                failure = transport.writer_failure
                assert failure is not None
                self.assertIs(failure.__cause__, write_error)

    def test_explicit_close_after_writer_fault_wakes_full_and_empty_readers(self):
        for full in (False, True):
            with self.subTest(full=full):
                error = OSError("fictional write")
                if full:
                    reader = ScriptedReader('{"id":1}\n{"id":2}\n')
                    writer = ControlledWriter(error=error)
                    process = SimpleNamespace(stdin=writer, stdout=reader, poll=lambda: 0)
                    transport, blocked, _ = self.transport(reader=reader, process=process)
                    self.addCleanup(writer.release.set)
                    self.assertTrue(blocked.wait(5))
                    transport.send({"id": "example-inflight"})
                    self.assertTrue(writer.entered.wait(5))
                else:
                    transport, writer, reader = self.blocked_writer_transport(error=error)
                    self.assertEqual(transport.receive(timeout=0), {"id": "example-accepted"})
                writer.release.set()
                transport._writer_thread.join(5)
                transport.close()
                self.assertFalse(transport._reader_thread.is_alive())
                self.assertFalse(transport._writer_thread.is_alive())
                if full:
                    self.assertEqual(transport.receive(timeout=0), {"id": 1})
                with self.assertRaises(OSError) as raised:
                    transport.receive(timeout=0)
                self.assertIs(raised.exception, error)

    def test_failure_polling_without_fault_keeps_the_original_quiet_deadline(self):
        reader = ScriptedReader("", wait_at_end=True)
        transport, _, _ = self.transport(reader=reader)
        clock = SimpleNamespace(now=0.0)
        waits = []

        def get(*, timeout):
            waits.append(timeout)
            clock.now += timeout
            raise queue.Empty

        with (
            patch.object(transport._inbound, "get", side_effect=get),
            patch(
                "hermes_codex_router.codex_transports.time",
                SimpleNamespace(monotonic=lambda: clock.now),
            ),
        ):
            with self.assertRaisesRegex(RpcError, "timed out"):
                transport.receive(timeout=3.0, response_remaining=lambda: float("inf"))
        self.assertEqual(waits, [1.0, 1.0, 1.0])
        self.assertIsNone(transport.writer_failure)

    def test_outbound_admission_serializes_an_immutable_snapshot(self):
        transport, writer, _ = self.blocked_writer_transport()
        message = {"id": "example-next", "params": {"text": "Original."}}
        transport.send(message)
        message["params"]["text"] = "Changed."
        delivered = threading.Event()

        def flush():
            if len(writer.writes) == 2:
                delivered.set()

        # Observe completion before releasing the sole stream owner.
        with patch.object(writer, "flush", side_effect=flush):
            writer.release.set()
            self.assertTrue(delivered.wait(1))
        self.assertEqual(json.loads(writer.writes[1])["params"]["text"], "Original.")

    def test_idle_successful_client_retains_bounded_old_frames_without_contaminating_next_turn(
        self,
    ):
        entered_idle = threading.Event()
        full = threading.Event()

        class IdleReader(ScriptedReader):
            def readline(self, size=-1):
                if self.text.tell() == first_end:
                    entered_idle.set()
                    if not self.release.wait(2):
                        raise AssertionError("idle fixture was not released")
                return super().readline(size)

        class IdleInbox(BoundedInbox):
            def put(self, value, size):
                if entered_idle.is_set() and self.pending_frames == 1:
                    full.set()
                return super().put(value, size)

        def turn_frames(request_id, turn_id, text):
            return [
                {"id": request_id, "result": {"turn": {"id": turn_id}}},
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "example-thread",
                        "turnId": turn_id,
                        "item": {"id": turn_id, "type": "agentMessage", "text": text},
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "example-thread",
                        "turn": {"id": turn_id},
                    },
                },
            ]

        first = "".join(
            json.dumps(frame) + "\n" for frame in turn_frames(1, "example-first", "First.")
        )
        first_end = len(first)
        old = [
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": "example-thread",
                    "turnId": "example-first",
                    "delta": "Stale.",
                },
            }
            for _ in range(1200)
        ]
        reader = IdleReader(
            first
            + "".join(
                json.dumps(frame) + "\n"
                for frame in [
                    *old,
                    *turn_frames(2, "example-second", "Second."),
                ]
            )
        )
        process = SimpleNamespace(stdin=io.StringIO(), stdout=reader, poll=lambda: 0)
        with patch("hermes_codex_router.codex_transports.BoundedInbox", IdleInbox):
            transport = StdioJsonLineTransport(
                cast(subprocess.Popen[str], process), max_pending_frames=1
            )
        self.addCleanup(transport.close)
        self.addCleanup(reader.release.set)
        client = CodexAppServerClient(transport, initialized=True)
        first_turn = client.start_turn(
            thread_id="example-thread", text="First", cwd=Path.cwd(), model="example", effort="high"
        )
        self.assertEqual(client.wait_for_turn(first_turn).text, "First.")
        self.assertTrue(entered_idle.wait(1))
        reader.release.set()
        self.assertTrue(full.wait(1))
        self.assertEqual(transport._inbound.pending_frames, 1)
        next_turn = client.start_turn(
            thread_id="example-thread",
            text="Second",
            cwd=Path.cwd(),
            model="example",
            effort="high",
        )
        self.assertEqual(client.wait_for_turn(next_turn).text, "Second.")
        self.assertFalse(client.notifications)

    def test_malformed_nonobject_and_deep_json_frames_keep_prior_frame(self):
        for malformed in ("{\n", "[]\n", "[" * 1200 + "]" * 1200 + "\n"):
            with self.subTest(prefix=malformed[:5]):
                transport, _, _ = self.transport(reader=ScriptedReader('{"id":1}\n' + malformed))
                self.assertEqual(transport.receive(timeout=1), {"id": 1})
                with self.assertRaises(RpcError):
                    transport.receive(timeout=1)

    def test_read_failure_drains_accepted_frames_and_retains_first_cause(self):
        error = OSError("fictional read failure")
        reader = ScriptedReader('{"id":1}\n', error=error)
        transport, _, _ = self.transport(reader=reader)
        transport._reader_thread.join(1)
        transport.close()
        self.assertEqual(transport.receive(timeout=0), {"id": 1})
        with self.assertRaises(OSError) as raised:
            transport.receive(timeout=0)
        self.assertIs(raised.exception, error)

    def test_multibyte_and_unterminated_oversized_frames_are_bounded(self):
        for oversized in ('{"text":"' + "x" * 1000, '{"text":"' + "\u0416" * 80 + '"}\n'):
            with self.subTest(multibyte="\u0416" in oversized):
                reader = ScriptedReader('{"id":1}\n' + oversized)
                transport, _, _ = self.transport(reader=reader, frame_bytes=128)
                self.assertEqual(transport.receive(timeout=1), {"id": 1})
                with self.assertRaisesRegex(RpcError, "inbound frame exceeded"):
                    transport.receive(timeout=1)
                self.assertTrue(all(0 < limit <= 129 for limit in reader.limits))

    def test_valid_multibyte_final_frame_without_newline_is_retained(self):
        reader = ScriptedReader('{"text":"\u041f\u0440\u0438\u0432\u0435\u0442"}')
        transport, _, _ = self.transport(reader=reader, frame_bytes=128)
        self.assertEqual(
            transport.receive(timeout=1), {"text": "\u041f\u0440\u0438\u0432\u0435\u0442"}
        )
        with self.assertRaises(EOFError):
            transport.receive(timeout=1)

    def test_close_wakes_full_producer_then_drains_only_accepted_frames(self):
        transport, full, reader = self.transport([{"id": 1}, {"id": 2}, {"id": 3}])
        self.assertTrue(full.wait(1))
        transport.close()
        transport.close()
        self.assertFalse(transport._reader_thread.is_alive())
        self.assertTrue(reader.closed)
        self.assertEqual(transport.receive(timeout=0), {"id": 1})
        with self.assertRaises(EOFError):
            transport.receive(timeout=0)

    def test_close_wakes_empty_consumer_and_stalled_pipe_reader(self):
        reader = ScriptedReader("", wait_at_end=True)
        process = SimpleNamespace(
            stdin=io.StringIO(),
            stdout=reader,
            poll=lambda: None,
            terminate=reader.release.set,
            wait=lambda timeout: 0,
        )
        transport, _, _ = self.transport(reader=reader, process=process)
        self.assertTrue(reader.at_end.wait(1))
        waiting = threading.Event()
        failures = []
        original_wait = transport._inbound._condition.wait

        def wait(timeout=None):
            waiting.set()
            return original_wait(timeout)

        def receive():
            try:
                transport.receive(timeout=2)
            except EOFError:
                failures.append("closed")

        with patch.object(transport._inbound._condition, "wait", side_effect=wait):
            consumer = threading.Thread(target=receive, daemon=True)
            consumer.start()
            self.assertTrue(waiting.wait(1))
            transport.close()
            consumer.join(1)
        self.assertFalse(consumer.is_alive())
        self.assertFalse(transport._reader_thread.is_alive())
        self.assertEqual(failures, ["closed"])

    def test_receive_timeout_does_not_poison_later_delivery(self):
        reader = ScriptedReader("", wait_at_end=True)
        transport, _, _ = self.transport(reader=reader)
        with self.assertRaisesRegex(RpcError, "timed out"):
            transport.receive(timeout=0)
        line = '{"id":"example-later"}\n'
        transport._inbound.put(line, len(line))
        self.assertEqual(transport.receive(timeout=1), {"id": "example-later"})
