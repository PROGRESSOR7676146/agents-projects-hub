from __future__ import annotations

import io
import logging
import unittest
from unittest.mock import patch

from hermes_codex_router import diagnostic_log


class TokenBearingError(RuntimeError):
    pass


class DiagnosticLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = io.StringIO()
        self.logger = logging.getLogger(diagnostic_log.LOGGER_NAME)
        self.saved = (list(self.logger.handlers), self.logger.level, self.logger.propagate)
        self.logger.handlers = [
            handler
            for handler in self.logger.handlers
            if not isinstance(handler, diagnostic_log.DiagnosticHandler)
        ]
        diagnostic_log.configure_process_logging(self.stream)
        diagnostic_log.reset_repeat_state()

    def tearDown(self) -> None:
        self.logger.handlers, level, self.logger.propagate = (
            self.saved[0],
            self.saved[1],
            self.saved[2],
        )
        self.logger.setLevel(level)
        diagnostic_log.reset_repeat_state()

    def test_record_names_class_and_site_but_never_exception_text(self) -> None:
        secret = "https://api.telegram.org/bot123456:ABCdefGHIjkl/sendMessage /home/example/x"
        diagnostic_log.survived("service.runtime_event_record", TokenBearingError(secret))
        output = self.stream.getvalue()
        self.assertIn("TokenBearingError", output)
        self.assertIn("service.runtime_event_record", output)
        self.assertNotIn("123456", output)
        self.assertNotIn("/home/example", output)
        self.assertNotIn("Traceback", output)

    def test_dynamic_site_text_is_not_logged(self) -> None:
        diagnostic_log.survived("service.topic -1001234567890", RuntimeError("x"))
        diagnostic_log.survived("/home/example/state.db", ValueError("x"))
        output = self.stream.getvalue()
        self.assertNotIn("1001234567890", output)
        self.assertNotIn("/home/example", output)
        self.assertEqual(output.count("invalid_site.unknown"), 2)

    def test_repeats_are_bounded_and_counted(self) -> None:
        clock = iter((0.0, 1.0, 2.0, 61.0))
        with patch.object(diagnostic_log.time, "monotonic", lambda: next(clock)):
            for _ in range(4):
                diagnostic_log.survived("telegram_activity.chat_action", OSError("x"))
        lines = self.stream.getvalue().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertNotIn("similar", lines[0])
        self.assertIn("+2 similar", lines[1])

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
        self.logger.propagate = True
        with patch.object(logging, "lastResort", None), patch("sys.stderr", io.StringIO()) as err:
            diagnostic_log.survived("service.outbox_cycle", RuntimeError("x"))
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
