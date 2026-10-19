"""Local control CLI boundaries, temporary fixtures only."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router.cli import main
from hermes_codex_router.worker_activity_barrier import WorkerActivityBarrier
from hermes_codex_router.worker_control_socket import WorkerControlEndpoint, worker_control_session
from hermes_codex_router.worker_drain_protocol import current_binding, drain_request
from tests.test_worker_control_socket import RELEASE


class WorkerControlCliTests(unittest.TestCase):
    def test_default_off_touches_no_worker_identity_or_control_files(self) -> None:
        with (
            patch(
                "hermes_codex_router.worker_control_socket.current_binding",
                side_effect=AssertionError,
            ),
            patch(
                "hermes_codex_router.worker_control_socket.WorkerControlEndpoint",
                side_effect=AssertionError,
            ),
            worker_control_session(
                None, worker_id="example-worker", worker=cast(Any, SimpleNamespace())
            ),
        ):
            pass

    def test_prepare_request_and_status_use_one_saved_exact_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            path, binding_file, request_file = (
                root / "control.sock",
                root / "binding.json",
                root / "request.json",
            )
            worker = SimpleNamespace(
                activity=WorkerActivityBarrier(), _process_start_marker="b" * 32
            )
            from hermes_codex_router.worker_drain_protocol import current_binding

            binding = current_binding(
                "example-worker", worker._process_start_marker, release=RELEASE
            )
            endpoint = WorkerControlEndpoint(path, binding, worker.activity)
            try:
                endpoint.start()
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(main(["worker-control", "inspect", str(path)]), 0)
                binding_file.write_text(output.getvalue())
                binding_file.chmod(0o600)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        main(
                            [
                                "worker-control",
                                "prepare",
                                str(binding_file),
                                "--request-id",
                                "c" * 32,
                                "--ttl-seconds",
                                "10",
                            ]
                        ),
                        0,
                    )
                request_file.write_text(output.getvalue())
                request_file.chmod(0o600)
                for command in ("status", "request", "status"):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        result = main(["worker-control", command, str(path), str(request_file)])
                    self.assertEqual(
                        result, 2 if command == "status" and not worker.activity.draining else 0
                    )
                self.assertTrue(worker.activity.drained)
                self.assertTrue(json.loads(output.getvalue())["operations_drained"])
            finally:
                endpoint.close()

    def test_unsafe_or_oversized_input_file_cannot_trigger_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            binding = current_binding("example-worker", "b" * 32, release=RELEASE)
            content = json.dumps(
                drain_request(binding, request_id="c" * 32, expires_ns=time.monotonic_ns() + 10**9)
            )
            with (
                patch(
                    "hermes_codex_router.worker_control_cli.exchange_control",
                    side_effect=AssertionError,
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                for name, text, mode, linked in (
                    ("oversized", content + " " * 5000, 0o600, False),
                    ("unsafe_mode", content, 0o644, False),
                    ("multiple_links", content, 0o600, True),
                ):
                    with self.subTest(name=name):
                        path.write_text(text)
                        path.chmod(mode)
                        if linked:
                            os.link(path, path.with_name("second.json"))
                        self.assertEqual(
                            main(["worker-control", "request", "/tmp/example.sock", str(path)]),
                            2,
                        )

    def test_cli_endpoint_lifecycle_remains_inside_slot_lock(self) -> None:
        events: list[str] = []

        @contextlib.contextmanager
        def lock(*_args: object):
            events.append("lock")
            yield
            events.append("unlock")

        @contextlib.contextmanager
        def control(*_args: object, **_kwargs: object):
            events.append("control_start")
            yield
            events.append("control_close")

        class Worker:
            def __init__(self, *_args: object) -> None:
                events.append("construct")

            def stop(self) -> None:
                pass

            def run_forever(self, **_kwargs: object) -> None:
                events.append("run")

            def close(self) -> None:
                events.append("worker_close")

        with (
            patch(
                "hermes_codex_router.cli.load_external_worker_config",
                return_value=SimpleNamespace(
                    state_path=Path("/tmp/example.sqlite3"),
                    worker_count_for_agent=lambda _: 1,
                ),
            ),
            patch("hermes_codex_router.cli.worker_process_lock", lock),
            patch("hermes_codex_router.cli.worker_control_session", control),
            patch("hermes_codex_router.cli.ExternalQueueWorker", Worker),
        ):
            self.assertEqual(
                main(
                    [
                        "worker",
                        "example.json",
                        "--drain-control-socket",
                        "/tmp/example/control.sock",
                    ]
                ),
                0,
            )
        self.assertEqual(
            events,
            [
                "lock",
                "construct",
                "control_start",
                "run",
                "control_close",
                "worker_close",
                "unlock",
            ],
        )
