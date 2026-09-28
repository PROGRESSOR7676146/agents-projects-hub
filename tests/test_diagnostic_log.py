from __future__ import annotations

import ast
import asyncio
import importlib
import io
import logging
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
import types
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router import diagnostic_log
from hermes_codex_router.state import StateError

PACKAGE = Path(__file__).resolve().parents[1] / "src" / "hermes_codex_router"
SECRET = "https://api.telegram.org/bot123456:ABCdefGHIjkl/sendMessage /home/example/x"


class TokenBearingError(RuntimeError):
    pass


class FailingStream(io.StringIO):
    def __init__(self, *, fail_write: bool = False, fail_flush: bool = False) -> None:
        super().__init__()
        self.fail_write = fail_write
        self.fail_flush = fail_flush

    def write(self, text: str) -> int:
        if self.fail_write:
            raise OSError("stream write failed")
        return super().write(text)

    def flush(self) -> None:
        if self.fail_flush:
            raise OSError("stream flush failed")
        super().flush()


class DiagnosticLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger(diagnostic_log.LOGGER_NAME)
        self.saved = (list(self.logger.handlers), self.logger.level, self.logger.propagate)
        self.use_stream(io.StringIO())
        self.addCleanup(self.restore)

    def use_stream(self, stream: io.StringIO) -> None:
        self.stream = stream
        self.logger.handlers = [
            handler
            for handler in self.logger.handlers
            if not isinstance(handler, diagnostic_log.DiagnosticHandler)
        ]
        diagnostic_log.configure_process_logging(stream)
        diagnostic_log.reset_repeat_state()

    def restore(self) -> None:
        self.logger.handlers = self.saved[0]
        self.logger.setLevel(self.saved[1])
        self.logger.propagate = self.saved[2]
        diagnostic_log.reset_repeat_state()

    def test_record_names_class_and_site_but_never_exception_text(self) -> None:
        diagnostic_log.survived("service.health_publish", sqlite3.OperationalError(SECRET))
        diagnostic_log.survived("service.client_close", StateError(SECRET))
        output = self.stream.getvalue()
        self.assertIn("survived OperationalError at service.health_publish", output)
        self.assertIn("survived StateError at service.client_close", output)
        self.assertNotIn("123456", output)
        self.assertNotIn("/home/example", output)
        self.assertNotIn("Traceback", output)

    def test_unregistered_or_dynamic_sites_are_not_logged(self) -> None:
        for site, error_type in zip(
            ("service.session_fictional_1234", "service.topic -1001234567890", "/home/example/db"),
            (RuntimeError, ValueError, KeyError),
        ):
            diagnostic_log.survived(site, error_type("x"))
        output = self.stream.getvalue()
        self.assertNotIn("fictional_1234", output)
        self.assertNotIn("1001234567890", output)
        self.assertNotIn("/home/example", output)
        self.assertEqual(output.count(diagnostic_log.INVALID_SITE), 3)

    def test_exception_class_names_must_be_plain_identifiers(self) -> None:
        weird = type("Bad\nname /home/example/token", (RuntimeError,), {})
        diagnostic_log.survived("service.health_publish", weird("x"))
        output = self.stream.getvalue()
        self.assertIn("survived RuntimeError at service.health_publish", output)
        self.assertNotIn("/home/example", output)
        self.assertEqual(len(output.splitlines()), 1)

    def test_a_class_built_at_run_time_never_names_the_record(self) -> None:
        dynamic = type("Session_fictional_1234_Error", (TimeoutError,), {})
        spoofed = type(
            "Session_fictional_5678_Error",
            (RuntimeError,),
            {"__module__": "hermes_codex_router.state"},
        )
        # A factory can also register the class under its own name, as pickle needs.
        factory = types.ModuleType("hermes_codex_router.fictional_factory")
        registered = type(
            "Session_fictional_9012_Error",
            (RuntimeError,),
            {"__module__": factory.__name__},
        )
        setattr(factory, registered.__name__, registered)
        self.addCleanup(sys.modules.pop, factory.__name__, None)
        sys.modules[factory.__name__] = factory
        diagnostic_log.survived("service.health_publish", dynamic("x"))
        diagnostic_log.survived("service.client_close", spoofed("x"))
        diagnostic_log.survived("service.context_telemetry", TokenBearingError("x"))
        diagnostic_log.survived("service.queue_error_record", registered("x"))
        output = self.stream.getvalue()
        self.assertIn("survived TimeoutError at service.health_publish", output)
        self.assertIn("survived RuntimeError at service.client_close", output)
        self.assertIn("survived RuntimeError at service.context_telemetry", output)
        self.assertIn("survived RuntimeError at service.queue_error_record", output)
        self.assertNotIn("fictional", output)
        self.assertNotIn("TokenBearingError", output)

    def test_class_supplied_text_never_reaches_the_log_or_raises(self) -> None:
        class Unhashable(str):
            __hash__ = None  # type: ignore[assignment]

        class Disguised(str):
            def __str__(self) -> str:
                return "Session_fictional_3456_Error /home/example/private"

        class RaisingMeta(type):
            # Fails on every metadata read, even with an exception that is not
            # an Exception subclass.
            def __getattribute__(cls, name: str) -> object:
                if name in {"__qualname__", "__module__", "__mro__", "__name__"}:
                    raise asyncio.CancelledError
                return super().__getattribute__(name)

        unhashable = type(
            "ExampleError",
            (RuntimeError,),
            {"__module__": "builtins", "__qualname__": Unhashable("RuntimeError")},
        )
        disguised = type(
            "ExampleError",
            (RuntimeError,),
            {"__module__": "builtins", "__qualname__": Disguised("RuntimeError")},
        )
        raising = cast(type[RuntimeError], RaisingMeta("ExampleError", (RuntimeError,), {}))
        armed = False

        class Key(str):
            # A namespace key whose comparison fails once the class exists.
            __hash__ = str.__hash__

            def __eq__(self, other: object) -> bool:
                if armed:
                    raise asyncio.CancelledError
                return str.__eq__(self, other)

        keyed = type("ExampleError", (RuntimeError,), {Key("__module__"): "example"})
        diagnostic_log.survived("service.health_publish", unhashable("x"))
        diagnostic_log.survived("service.client_close", disguised("x"))
        diagnostic_log.survived("service.context_telemetry", raising("x"))
        keyed_error = keyed("x")
        armed = True
        diagnostic_log.survived("service.outbox_error_record", keyed_error)
        armed = False
        site = Disguised("service.queue_error_record")
        diagnostic_log.survived(site, RuntimeError("x"))
        output = self.stream.getvalue()
        self.assertIn("survived RuntimeError at service.health_publish", output)
        self.assertIn("survived RuntimeError at service.client_close", output)
        self.assertIn("survived RuntimeError at service.context_telemetry", output)
        self.assertIn("survived RuntimeError at service.outbox_error_record", output)
        self.assertIn(f"survived RuntimeError at {diagnostic_log.INVALID_SITE}", output)
        self.assertNotIn("fictional", output)
        self.assertNotIn("/home/example", output)

    def test_every_registered_name_is_a_real_exception_class(self) -> None:
        for module_name, name in sorted(diagnostic_log.NAMED_ERRORS):
            with self.subTest(module=module_name, name=name):
                value = getattr(importlib.import_module(module_name), name)
                self.assertTrue(isinstance(value, type) and issubclass(value, BaseException))
                if module_name != "builtins":
                    self.assertEqual((value.__module__, value.__qualname__), (module_name, name))

    def test_failing_log_stream_is_silent_and_never_raises(self) -> None:
        streams = (
            FailingStream(fail_write=True),
            FailingStream(fail_flush=True),
            io.StringIO(),
        )
        streams[2].close()
        for stream in streams:
            with self.subTest(stream=stream):
                self.use_stream(stream)
                captured = io.StringIO()
                with patch("sys.stderr", captured):
                    try:
                        raise TokenBearingError(SECRET)
                    except TokenBearingError as error:
                        diagnostic_log.survived("service.health_publish", error)
                self.assertEqual(captured.getvalue(), "")
                self.assertEqual(diagnostic_log.dropped_records(), 1)

    def test_forked_child_can_log_while_the_parent_holds_the_lock(self) -> None:
        def child() -> None:
            diagnostic_log.survived("service.health_publish", RuntimeError("x"))

        with diagnostic_log._lock:
            process = multiprocessing.get_context("fork").Process(target=child)
            process.start()
            process.join(10)
            if process.is_alive():
                process.kill()
                process.join(2)
        self.assertEqual(process.exitcode, 0, "forked child deadlocked on the diagnostic lock")

    def test_forked_child_logs_while_a_parent_thread_holds_the_stream_buffer(self) -> None:
        read_fd, write_fd = os.pipe()
        stream = open(write_fd, "w", buffering=1 << 16, encoding="utf-8")
        self.addCleanup(stream.close)
        self.use_stream(cast(Any, stream))
        drained = threading.Event()

        def fill_pipe() -> None:
            # Blocks inside the buffered write, holding its lock, until drained.
            stream.write("x" * 200_000)
            stream.flush()

        def child() -> None:
            diagnostic_log.survived("service.health_publish", RuntimeError("x"))

        def drain() -> None:
            with open(read_fd, "rb") as reader:
                while reader.read(65536):
                    pass
            drained.set()

        writer = threading.Thread(target=fill_pipe, daemon=True)
        writer.start()
        time.sleep(0.5)
        process = multiprocessing.get_context("fork").Process(target=child)
        process.start()
        threading.Thread(target=drain, daemon=True).start()
        process.join(10)
        if process.is_alive():
            process.kill()
            process.join(2)
        writer.join(10)
        self.assertEqual(process.exitcode, 0, "forked child deadlocked on the stream buffer")

    def test_forked_child_exits_normally_while_a_parent_thread_holds_the_stream_buffer(
        self,
    ) -> None:
        # A normal exit runs logging.shutdown(), which flushes every handler.
        script = textwrap.dedent(
            """
            import os, sys, threading, time
            from hermes_codex_router import diagnostic_log

            read_fd, write_fd = os.pipe()
            stream = open(write_fd, "w", buffering=1 << 16, encoding="utf-8")
            diagnostic_log.configure_process_logging(stream)

            def fill_pipe():
                # Blocks inside the buffered write, holding its lock, until drained.
                stream.write("x" * 200_000)
                stream.flush()

            threading.Thread(target=fill_pipe, daemon=True).start()
            time.sleep(0.5)
            child = os.fork()
            if child == 0:
                diagnostic_log.survived("service.health_publish", RuntimeError("x"))
                sys.exit(0)

            def drain():
                with open(read_fd, "rb") as reader:
                    while reader.read(65536):
                        pass

            threading.Thread(target=drain, daemon=True).start()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                done, status = os.waitpid(child, os.WNOHANG)
                if done:
                    os._exit(os.waitstatus_to_exitcode(status))
                time.sleep(0.05)
            os.kill(child, 9)
            os.waitpid(child, 0)
            os._exit(3)
            """
        )
        environment = {**os.environ, "PYTHONPATH": str(PACKAGE.parent)}
        completed = subprocess.run(
            [sys.executable, "-c", script],
            env=environment,
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, "forked child hung at its normal exit")

    def test_repeats_are_bounded_and_counted(self) -> None:
        clock = iter((0.0, 1.0, 2.0, 61.0))
        with patch.object(diagnostic_log.time, "monotonic", lambda: next(clock)):
            for _ in range(4):
                diagnostic_log.survived("telegram_activity.message_draft", OSError("x"))
        lines = self.stream.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertNotIn("similar", lines[0])
        self.assertIn("+2 similar", lines[1])

    def test_repeat_state_has_a_bounded_number_of_keys(self) -> None:
        for index in range(diagnostic_log.MAX_REPEAT_KEYS + 50):
            error_type = type(f"GeneratedError{index}", (RuntimeError,), {})
            diagnostic_log.survived("service.health_publish", error_type("x"))
        self.assertLessEqual(len(diagnostic_log._last_emitted), diagnostic_log.MAX_REPEAT_KEYS + 1)

    def test_survived_runtime_failure_is_visible_without_its_text(self) -> None:
        from hermes_codex_router import telegram_activity

        class Telegram:
            def send_chat_action(self, *_args: object) -> None:
                pass

            def send_message_draft(self, *_args: object, **_kwargs: object) -> None:
                raise RuntimeError("bot123456:ABCdefGHIjkl rejected draft for chat 42")

        telegram_activity._publish(Telegram(), 42, 0, 7)  # type: ignore[arg-type]
        output = self.stream.getvalue()
        self.assertIn("survived RuntimeError at telegram_activity.message_draft", output)
        self.assertNotIn("bot123456", output)
        self.assertNotIn("chat 42", output)

    def test_configuration_is_idempotent_and_library_use_is_silent(self) -> None:
        diagnostic_log.configure_process_logging(self.stream)
        handlers = [
            handler
            for handler in self.logger.handlers
            if isinstance(handler, diagnostic_log.DiagnosticHandler)
        ]
        self.assertEqual(len(handlers), 1)
        self.assertFalse(self.logger.propagate)
        self.logger.handlers = [
            handler
            for handler in self.logger.handlers
            if not isinstance(handler, diagnostic_log.DiagnosticHandler)
        ]
        with patch("sys.stderr", io.StringIO()) as err:
            diagnostic_log.survived("service.outbox_error_record", RuntimeError("x"))
        self.assertEqual(err.getvalue(), "")

    def test_every_call_site_uses_exactly_the_registered_labels(self) -> None:
        used: set[str] = set()
        for path in PACKAGE.glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "survived"
                ):
                    first = node.args[0]
                    self.assertIsInstance(first, ast.Constant, f"{path.name}:{node.lineno}")
                    assert isinstance(first, ast.Constant)
                    used.add(str(first.value))
        self.assertEqual(used, set(diagnostic_log.SITES))


if __name__ == "__main__":
    unittest.main()
