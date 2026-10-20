"""Real local sockets and fictional workers; no installed state or providers."""

from __future__ import annotations

import errno
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.release_identity import ReleaseIdentity
from hermes_codex_router.worker_activity_barrier import WorkerActivityBarrier
from hermes_codex_router.worker_control_socket import (
    WorkerControlEndpoint,
    exchange_control,
    inspect_control,
    worker_control_session,
)
from hermes_codex_router.worker_drain_protocol import (
    ControlBinding,
    ControlError,
    current_binding,
    decode_document,
    drain_request,
    encode_frame,
    request_digest,
    status_request,
)
from tests import test_external_worker as fixtures

RELEASE = ReleaseIdentity("0.0.0", "a" * 40, "2026-10-01T00:00:00+00:00", True)


class WorkerControlSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.root.chmod(0o700)
        self.path = self.root / "control.sock"
        self.binding = current_binding("example-worker", "b" * 32, release=RELEASE)
        self.activity = WorkerActivityBarrier()

    def endpoint(self) -> WorkerControlEndpoint:
        endpoint = WorkerControlEndpoint(self.path, self.binding, self.activity)
        endpoint.start()
        self.addCleanup(endpoint.close)
        return endpoint

    def request(self, *, expires_ns: int | None = None) -> dict[str, object]:
        return drain_request(
            self.binding,
            request_id="c" * 32,
            expires_ns=expires_ns or time.monotonic_ns() + 30_000_000_000,
        )

    def test_inspect_does_not_drain_and_binds_kernel_process(self) -> None:
        self.endpoint()
        result = inspect_control(self.path)
        self.assertEqual(result["binding"], self.binding.as_dict())
        self.assertFalse(result["accepted"])
        self.assertFalse(result["operations_drained"])
        self.assertFalse(self.activity.draining)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_busy_operation_finishes_but_next_admission_is_refused(self) -> None:
        self.endpoint()
        entered, release = threading.Event(), threading.Event()

        def productive() -> None:
            with self.activity.operation() as admitted:
                self.assertTrue(admitted)
                entered.set()
                release.wait(3)

        thread = threading.Thread(target=productive)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(release.set)
        self.assertTrue(entered.wait(2))
        request = self.request()
        result = exchange_control(self.path, self.binding, request)
        self.assertTrue(result["accepted"])
        self.assertFalse(result["operations_drained"])
        self.assertEqual(result["scope"], "tracked_operations_and_socket_transport_threads")
        self.assertEqual(result["request_digest"], request_digest(request))
        with self.activity.operation() as admitted:
            self.assertFalse(admitted)
        release.set()
        thread.join(2)
        result = exchange_control(self.path, self.binding, status_request(request))
        self.assertTrue(result["operations_drained"])
        self.assertNotIn("native_quiet", result)
        self.assertNotIn("stop_authorized", result)

    def test_tracked_thread_keeps_drain_unconfirmed_until_physical_exit(self) -> None:
        self.endpoint()
        release = threading.Event()
        thread = self.activity.thread(target=lambda: release.wait(3))
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(release.set)
        request = self.request()
        self.assertFalse(exchange_control(self.path, self.binding, request)["operations_drained"])
        release.set()
        thread.join(2)
        self.assertTrue(
            exchange_control(self.path, self.binding, status_request(request))["operations_drained"]
        )

    def test_lost_reply_has_read_only_status_and_idempotent_exact_repeat(self) -> None:
        self.endpoint()
        request = self.request()
        with patch.object(
            self.activity, "request_drain", wraps=self.activity.request_drain
        ) as drain:
            with patch(
                "hermes_codex_router.worker_control_socket._send_frame",
                side_effect=OSError("fictional lost response"),
            ):
                with self.assertRaises(ControlError):
                    exchange_control(self.path, self.binding, request)
            result = exchange_control(self.path, self.binding, status_request(request))
            self.assertTrue(result["accepted"])
            self.assertEqual(drain.call_count, 1)
            self.assertTrue(
                exchange_control(self.path, self.binding, request)["operations_drained"]
            )
            self.assertEqual(drain.call_count, 1)

    def test_expired_or_different_request_never_reopens_or_replaces_drain(self) -> None:
        self.endpoint()
        with self.assertRaises(ControlError):
            exchange_control(
                self.path, self.binding, self.request(expires_ns=time.monotonic_ns() - 1)
            )
        self.assertFalse(self.activity.draining)
        request = self.request()
        exchange_control(self.path, self.binding, request)
        changed = dict(request, request_id="d" * 32)
        with self.assertRaises(ControlError):
            exchange_control(self.path, self.binding, changed)
        self.assertTrue(self.activity.drained)
        self.assertTrue(
            exchange_control(self.path, self.binding, status_request(request))["accepted"]
        )

    def test_old_binding_and_wrong_process_are_refused_before_mutation(self) -> None:
        self.endpoint()
        for binding in (
            replace(self.binding, instance_nonce="d" * 32),
            replace(self.binding, pid=os.getpid() + 1),
            replace(self.binding, start_ticks=self.binding.start_ticks + 1),
            replace(self.binding, boot_id="00000000-0000-4000-8000-000000000001"),
            replace(self.binding, release=replace(RELEASE, git_sha="d" * 40)),
        ):
            with self.subTest(binding=binding), self.assertRaises(ControlError):
                exchange_control(
                    self.path,
                    binding,
                    drain_request(
                        binding, request_id="c" * 32, expires_ns=time.monotonic_ns() + 10**9
                    ),
                )
            self.assertFalse(self.activity.draining)

    def test_wrong_uid_malformed_duplicate_and_oversized_input_refused(self) -> None:
        self.endpoint()
        with (
            patch(
                "hermes_codex_router.worker_control_socket._peer_credentials",
                return_value=(os.getpid(), os.geteuid() + 1, os.getegid()),
            ),
            self.assertRaises(ControlError),
        ):
            exchange_control(self.path, self.binding, self.request())
        valid = encode_frame(self.request())
        duplicate = b'{"request_id":"' + b"d" * 32 + b'",' + valid[1:]
        oversized = valid[:-1] + b" " * 4096 + b"\n"
        # Both would close admission if accepted; rejection cannot rely on a
        # missing binding, an invalid operation, or malformed JSON syntax.
        for frame in (
            duplicate,
            oversized,
            b"[]\n",
            b'{"version":true,"operation":"inspect"}\n',
            b'{"version":2,"operation":"resume"}\n',
            b'{"version":1,"operation":"inspect"}\n',
        ):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(self.path))
                client.sendall(frame)
                try:
                    client.recv(4096)
                except ConnectionResetError:
                    pass
            self.assertFalse(self.activity.draining)

    def test_document_bound_rejects_otherwise_valid_json(self) -> None:
        request = self.request()
        data = encode_frame(request)
        self.assertEqual(decode_document(data), request)
        with self.assertRaises(ControlError):
            decode_document(data + b" " * 4096)

    def test_session_uses_real_worker_nonce_and_actual_binding_builder(self) -> None:
        fixture = fixtures.ExternalQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        worker = fixture.worker("opencode", fixtures.Adapter("opencode"))
        self.addCleanup(worker.close)
        # Substitute only build identity, keeping the real builder, worker,
        # constructor-generated nonce, process identity and endpoint lifecycle.
        with (
            patch(
                "hermes_codex_router.worker_control_socket.current_binding",
                partial(current_binding, release=RELEASE),
            ),
            worker_control_session(self.path, worker_id=worker.worker_id, worker=worker),
        ):
            binding = ControlBinding.parse(inspect_control(self.path)["binding"])
            self.assertEqual(binding.instance_nonce, worker._process_start_marker)
            self.assertEqual(binding.worker_id, worker.worker_id)
            self.assertEqual(binding.pid, os.getpid())
            self.assertEqual(binding.release, RELEASE)
            self.assertFalse(worker.activity.draining)
        self.assertFalse(self.path.exists())

    def test_session_refuses_worker_id_mismatch_before_binding_or_socket_io(self) -> None:
        fixture = fixtures.ExternalQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        worker = fixture.worker("opencode", fixtures.Adapter("opencode"))
        self.addCleanup(worker.close)
        with (
            patch(
                "hermes_codex_router.worker_control_socket.current_binding",
                side_effect=AssertionError("binding must not be read"),
            ),
            self.assertRaises(ControlError),
            worker_control_session(self.path, worker_id="wrong-worker", worker=worker),
        ):
            self.fail("mismatched worker entered")
        self.assertFalse(self.path.exists())
        self.assertFalse(worker.activity.draining)

    def test_post_bind_identity_failure_preserves_unproven_socket(self) -> None:
        original = os.stat
        rejected = False

        def stat_path(path, *args, **kwargs):
            nonlocal rejected
            if path == self.path.name and "dir_fd" in kwargs and not rejected:
                rejected = True
                raise OSError("fictional failure before inode capture")
            return original(path, *args, **kwargs)

        with patch("hermes_codex_router.worker_control_socket.os.stat", stat_path):
            with self.assertRaises(ControlError):
                self.endpoint()
        self.assertTrue(rejected)
        self.assertTrue(self.path.is_socket())
        self.assertFalse(self.activity.draining)

    def test_unsupported_no_follow_chmod_refuses_opt_in_and_cleans_created_socket(self) -> None:
        with (
            patch(
                "hermes_codex_router.worker_control_socket.os.chmod",
                side_effect=NotImplementedError("fictional unsupported host"),
            ),
            self.assertRaises(ControlError),
        ):
            self.endpoint()
        self.assertFalse(self.path.exists())
        self.assertFalse(self.activity.draining)

    def test_partial_client_cannot_block_listener_indefinitely(self) -> None:
        self.endpoint()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as slow:
            slow.settimeout(2)
            slow.connect(str(self.path))
            slow.sendall(b"{")
            self.assertFalse(json.loads(slow.recv(4096))["ok"])
            self.assertFalse(inspect_control(self.path)["accepted"])

    def test_transient_accept_failure_after_drain_preserves_exact_status(self) -> None:
        endpoint = self.endpoint()
        request = self.request()
        exchange_control(self.path, self.binding, request)
        failed = threading.Event()
        original = socket.socket.accept

        def accept(listener: socket.socket):
            if listener is endpoint._socket and not failed.is_set():
                failed.set()
                raise OSError(errno.EMFILE, "fictional transient descriptor pressure")
            return original(listener)

        with patch.object(socket.socket, "accept", accept):
            self.assertTrue(failed.wait(2))
            result = exchange_control(self.path, self.binding, status_request(request))
        self.assertTrue(result["accepted"])
        self.assertTrue(result["operations_drained"])
        self.assertEqual(result["request_digest"], request_digest(request))

    def test_repeated_transient_accept_failure_can_still_close_listener(self) -> None:
        endpoint = self.endpoint()
        twice = threading.Event()
        attempts = 0
        original = socket.socket.accept

        def accept(listener: socket.socket):
            nonlocal attempts
            if listener is endpoint._socket:
                attempts += 1
                if attempts >= 2:
                    twice.set()
                raise OSError(errno.ENOBUFS, "fictional persistent buffer pressure")
            return original(listener)

        with patch.object(socket.socket, "accept", accept):
            self.assertTrue(twice.wait(2))
            endpoint.close()
        self.assertFalse(endpoint.alive)
        self.assertFalse(self.path.exists())

    def test_existing_path_is_preserved_and_replaced_path_is_not_unlinked(self) -> None:
        self.path.write_text("foreign")
        with self.assertRaises(ControlError):
            self.endpoint()
        self.assertEqual(self.path.read_text(), "foreign")
        self.path.unlink()
        endpoint = self.endpoint()
        self.path.unlink()
        self.path.write_text("replacement")
        endpoint.close()
        self.assertEqual(self.path.read_text(), "replacement")

    def test_private_canonical_directory_and_verified_release_required(self) -> None:
        self.root.chmod(0o755)
        with self.assertRaises(ControlError):
            self.endpoint()
        self.root.chmod(0o700)
        link = self.root / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ControlError):
            WorkerControlEndpoint(link / "other.sock", self.binding, self.activity).start()
        with self.assertRaises(ControlError):
            current_binding("example-worker", "b" * 32, release=replace(RELEASE, clean_tree=False))

    def test_close_joins_listener_and_cleans_only_its_socket(self) -> None:
        endpoint = self.endpoint()
        endpoint.close()
        self.assertFalse(self.path.exists())
        self.assertFalse(endpoint.alive)
        endpoint.close()
        self.assertFalse(self.activity.draining)

    def test_real_worker_persists_current_result_and_never_claims_tail(self) -> None:
        fixture = fixtures.ExternalQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.addCleanup(fixture.tearDown)
        head, tail = fixture.enqueue("opencode", 601), fixture.enqueue("opencode", 602)
        adapter = fixtures.Adapter("opencode")
        worker = fixture.worker("opencode", adapter)
        self.addCleanup(worker.close)
        self.activity = worker.activity
        self.endpoint()
        original = adapter.run_turn

        def turn(**kwargs: object):
            receipt = exchange_control(self.path, self.binding, self.request())
            self.assertTrue(receipt["accepted"])
            self.assertFalse(receipt["operations_drained"])
            self.assertFalse(worker._stop.is_set())
            return original(**kwargs)

        with patch.object(adapter, "run_turn", turn):
            self.assertTrue(worker.run_cycle())
        self.assertFalse(worker.run_cycle())
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(worker.state.get_provider_job(head).status, "result_ready")
        self.assertEqual(worker.state.get_provider_job(tail).status, "queued")
        self.assertTrue(self.activity.drained)

    def test_server_independently_rejects_wrong_uid_and_binding(self) -> None:
        self.endpoint()

        def raw(request: dict[str, object]) -> dict[str, object]:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(str(self.path))
                client.sendall(encode_frame(request))
                return json.loads(client.recv(4096))

        with patch(
            "hermes_codex_router.worker_control_socket._peer_credentials",
            return_value=(os.getpid(), os.geteuid() + 1, 1),
        ):
            self.assertFalse(raw(self.request())["ok"])
        request = self.request()
        request["binding"] = replace(self.binding, pid=os.getpid() + 1).as_dict()
        self.assertFalse(raw(request)["ok"])
        self.assertFalse(self.activity.draining)

    def test_expiry_applies_only_to_first_acceptance(self) -> None:
        self.endpoint()
        now = time.monotonic_ns()
        request = self.request(expires_ns=now + 30_000_000_000)
        with patch("hermes_codex_router.worker_control_socket.time.monotonic_ns") as clock:
            clock.return_value = now
            exchange_control(self.path, self.binding, request)
            clock.return_value = now + 31_000_000_000
            self.assertTrue(
                exchange_control(self.path, self.binding, request)["operations_drained"]
            )
            self.assertTrue(
                exchange_control(self.path, self.binding, status_request(request))["accepted"]
            )

    def test_wrong_digest_and_forged_positive_reply_are_unconfirmed(self) -> None:
        endpoint = self.endpoint()
        request = self.request()
        exchange_control(self.path, self.binding, request)
        with self.assertRaises(ControlError):
            exchange_control(
                self.path, self.binding, dict(status_request(request), request_digest="e" * 64)
            )
        dispatch = endpoint._dispatch

        def forged(request: dict[str, object]) -> dict[str, object]:
            return dict(dispatch(request), operations_drained=1)

        with patch.object(endpoint, "_dispatch", forged), self.assertRaises(ControlError):
            exchange_control(self.path, self.binding, status_request(request))

    def test_changed_parent_custody_refuses_status_without_reopening(self) -> None:
        self.endpoint()
        request = self.request()
        exchange_control(self.path, self.binding, request)
        self.root.chmod(0o755)
        with self.assertRaises(ControlError):
            exchange_control(self.path, self.binding, status_request(request))
        self.assertTrue(self.activity.drained)

    def test_stale_socket_is_refused_and_preserved(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(self.path))
        before = self.path.lstat()
        with self.assertRaises(ControlError):
            self.endpoint()
        self.assertEqual(self.path.lstat().st_ino, before.st_ino)

    def test_replaced_parent_is_not_used_for_status_or_cleanup(self) -> None:
        parent = self.root / "endpoint"
        parent.mkdir(mode=0o700)
        self.path = parent / "control.sock"
        endpoint = self.endpoint()
        parent.rename(self.root / "moved")
        parent.mkdir(mode=0o700)
        replacement = parent / "control.sock"
        replacement.write_text("foreign")
        with self.assertRaises(ControlError):
            inspect_control(self.root / "moved" / "control.sock")
        endpoint.close()
        self.assertEqual(replacement.read_text(), "foreign")
        self.assertFalse((self.root / "moved" / "control.sock").exists())

    def test_join_timeout_does_not_claim_listener_shutdown_or_release_path(self) -> None:
        endpoint = self.endpoint()
        assert endpoint._thread is not None
        with (
            patch.object(endpoint._thread, "join"),
            patch.object(endpoint._thread, "is_alive", return_value=True),
            self.assertRaises(ControlError),
        ):
            endpoint.close()
        self.assertTrue(self.path.exists())
        endpoint.close()
        self.assertFalse(self.path.exists())

    def test_exception_after_admission_closure_keeps_exact_status_observable(self) -> None:
        self.endpoint()
        request = self.request()
        drain = self.activity.request_drain

        def fail_after_close() -> None:
            drain()
            raise RuntimeError("fictional lost acceptance reply")

        with (
            patch.object(self.activity, "request_drain", fail_after_close),
            self.assertRaises(ControlError),
        ):
            exchange_control(self.path, self.binding, request)
        self.assertTrue(
            exchange_control(self.path, self.binding, status_request(request))["accepted"]
        )

    def test_real_peer_pid_and_process_replacement_cannot_retarget_old_request(self) -> None:
        def child() -> subprocess.Popen[str]:
            process = subprocess.Popen(
                [sys.executable, "-m", "tests.worker_control_fixture", str(self.path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            # communicate has a deadline; the fixture deliberately handles no input
            # except EOF/one line, and invokes no provider or installed component.
            self.addCleanup(finish, process)
            assert process.stdout is not None
            import select

            self.assertTrue(select.select([process.stdout], [], [], 5)[0])
            self.assertEqual(process.stdout.readline(), "ready\n")
            return process

        def finish(process: subprocess.Popen[str]) -> None:
            if process.poll() is None:
                try:
                    process.communicate("done\n", timeout=5)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    process.communicate(timeout=5)
            else:
                process.communicate(timeout=5)

        first = child()
        binding = ControlBinding.parse(inspect_control(self.path)["binding"])
        self.assertEqual(binding.pid, first.pid)
        self.assertNotEqual(binding.pid, os.getpid())
        request = drain_request(
            binding, request_id="c" * 32, expires_ns=time.monotonic_ns() + 10**9
        )
        self.assertTrue(exchange_control(self.path, binding, request)["operations_drained"])
        finish(first)
        second = child()
        with self.assertRaises(ControlError):
            exchange_control(self.path, binding, status_request(request))
        observed = inspect_control(self.path)
        self.assertEqual(ControlBinding.parse(observed["binding"]).pid, second.pid)
        self.assertFalse(observed["accepted"])
