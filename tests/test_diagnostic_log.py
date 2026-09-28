from __future__ import annotations

import ast
import importlib
import io
import logging
import sqlite3
import sys
import types
import unittest
from pathlib import Path
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
