"""Owner-facing notice copy contract (ADR 0106).

Each notice is Russian, says what happened, what it means and one action, and
stays short: at most 220 visible characters for a status, 400 for a failure or
blocker. Incomplete provider text is collapsed and escaped.
"""

from __future__ import annotations

import html
import re
import unittest

from hermes_codex_router.approval_observations import APPROVAL_CATEGORIES, approval_notice_html
from hermes_codex_router.claude_activity import NOTICE_COPY
from hermes_codex_router.claude_stream import _terminal_failure_message
from hermes_codex_router.codex_failure import (
    MAX_PARTIAL_TEXT,
    CodexPermissionPolicyDriftError,
    CodexPermissionProfileError,
    CodexPreparationError,
    CodexRetryBindingError,
    UnsupportedCodexPermissionProfileError,
    codex_failure_notice,
    partial_response_block,
    uncertain_provider_notice,
)
from hermes_codex_router.codex_ingress_control import INGRESS_PRECAUTION_NOTICE
from hermes_codex_router.delivery_wait import delivery_wait_explanation
from hermes_codex_router.notice_copy import _NOT_STARTED, limit_notice, not_started_notice
from hermes_codex_router.preexecution_retry_state import (
    _UNSAVED_REPLACEMENT_CONTEXT,
    RETRY_BINDING_CHANGED,
    RETRY_INPUT_MISSING,
    RETRY_MATERIALS,
    RETRY_OFFERED,
    RETRY_ROOT_UNVERIFIED,
)
from hermes_codex_router.task_activity import no_progress_text
from hermes_codex_router.telegram_multipart import TELEGRAM_HTML_LIMIT, split_telegram_html

JOB_ID = "00000000-0000-4000-8000-000000000001"
LONG_NAME = "Example Agent With A Long Display Name"

STATUS_LIMIT = 220
FAILURE_LIMIT = 400


def visible(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))


class NoticeCopyTests(unittest.TestCase):
    def assert_copy(self, text: str, limit: int) -> None:
        shown = visible(text)
        self.assertLessEqual(len(shown), limit, shown)
        self.assertRegex(shown, "[А-Яа-яЁё]")
        for english in ("What happened", "Saved:", "Next:", "provider work", "Hub will"):
            self.assertNotIn(english, shown)

    def test_failure_and_uncertainty_notices_are_short_russian(self) -> None:
        errors: list[BaseException] = [
            CodexRetryBindingError("private"),
            CodexPermissionProfileError(),
            UnsupportedCodexPermissionProfileError(),
            CodexPreparationError("private"),
            CodexPermissionPolicyDriftError(),
            ConnectionError("closed"),
            TimeoutError("timed out"),
            RuntimeError("429 too many requests"),
            RuntimeError("private provider detail"),
        ]
        for error in errors:
            for status in ("unknown", "failed", "interrupted"):
                with self.subTest(error=type(error).__name__, status=status):
                    notice = codex_failure_notice(
                        error, turn_status=status, held_count=3, protective_interrupt_attempted=True
                    )
                    self.assert_copy(notice, FAILURE_LIMIT)
                    self.assertNotIn("private", notice)
        self.assert_copy(uncertain_provider_notice(LONG_NAME, JOB_ID), FAILURE_LIMIT)
        for code in (
            "claude_quota_exhausted",
            "claude_rate_limited",
            "claude_authentication_failed",
            "claude_billing_error",
            "claude_model_not_found",
            "claude_provider_overloaded",
            "claude_provider_unavailable",
            "claude_turn_limit",
            "claude_budget_exhausted",
            "claude_structured_output_failed",
            "claude_provider_failure",
        ):
            with self.subTest(code=code):
                self.assert_copy(_terminal_failure_message(code), FAILURE_LIMIT)
        for code in (*_NOT_STARTED, "unknown_code"):
            with self.subTest(code=code):
                self.assert_copy(not_started_notice("Claude", code, ""), FAILURE_LIMIT)
        self.assert_copy(INGRESS_PRECAUTION_NOTICE, FAILURE_LIMIT)
        for enabled in (False, True):
            self.assert_copy(
                delivery_wait_explanation(hub_owner_recovery_enabled=enabled), FAILURE_LIMIT
            )

    def test_status_notices_fit_the_status_limit(self) -> None:
        self.assert_copy(NOTICE_COPY, STATUS_LIMIT)
        self.assert_copy(no_progress_text("tool"), STATUS_LIMIT)
        self.assert_copy(no_progress_text("ordinary"), STATUS_LIMIT)
        self.assert_copy(limit_notice("Claude"), STATUS_LIMIT)
        for category in APPROVAL_CATEGORIES:
            self.assert_copy(approval_notice_html(category), STATUS_LIMIT)

    def test_indeterminate_notice_never_promises_a_replay(self) -> None:
        for notice in (
            uncertain_provider_notice("Claude", JOB_ID),
            codex_failure_notice(RuntimeError("closed")),
        ):
            self.assertIn("Итог неизвестен", notice)
            self.assertIn("Автоповтора не будет", notice)
            self.assertIn("на паузе", notice)
            # A new message is refused while the project is paused; never suggest it.
            self.assertNotIn("новое сообщение", notice)

    def test_uncertain_notice_names_the_supported_owner_decision(self) -> None:
        notice = uncertain_provider_notice("Claude", JOB_ID)
        self.assertIn(f"<code>indeterminate-resolve {JOB_ID}</code>", notice)
        self.assertIn("без повтора", notice)

    def test_assembled_preexecution_notices_stay_within_limit(self) -> None:
        bases = (
            codex_failure_notice(CodexPreparationError("private")),
            codex_failure_notice(CodexRetryBindingError("private")),
        )
        suffixes = (
            RETRY_MATERIALS,
            RETRY_ROOT_UNVERIFIED,
            RETRY_BINDING_CHANGED,
            RETRY_INPUT_MISSING,
            RETRY_OFFERED,
            "\n" + _UNSAVED_REPLACEMENT_CONTEXT,
        )
        for base in bases:
            for suffix in suffixes:
                with self.subTest(suffix=suffix[:30]):
                    self.assert_copy(base + suffix, FAILURE_LIMIT)
        held = (
            codex_failure_notice(RuntimeError("closed"), turn_status="failed", held_count=999)
            + "\nЗапросов на паузе до вашего решения: 999."
        )
        self.assert_copy(held, FAILURE_LIMIT)

    def test_maximal_partial_splits_into_valid_collapsed_parts(self) -> None:
        partial = ("Fictional <tag> & 😀 текст " * 2000)[: MAX_PARTIAL_TEXT - 40]
        error = RuntimeError("closed")
        error.partial_text = partial  # type: ignore[attr-defined]
        parts = split_telegram_html(codex_failure_notice(error))
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), TELEGRAM_HTML_LIMIT)
            self.assertEqual(part.count("<blockquote expandable>"), part.count("</blockquote>"))
        self.assertNotIn("<blockquote", parts[0].split("<b>Частичный ответ")[0])
        self.assertTrue(all(part.startswith("<blockquote expandable>") for part in parts[1:]))
        joined = "".join(parts).replace("</blockquote><blockquote expandable>", "")
        self.assertIn(html.escape(partial.strip()), joined)

    def test_partial_response_is_collapsed_escaped_and_after_the_action(self) -> None:
        self.assertEqual(partial_response_block("   "), "")
        block = partial_response_block("<b>draft</b> & more")
        self.assertIn(
            "<blockquote expandable>&lt;b&gt;draft&lt;/b&gt; &amp; more</blockquote>", block
        )
        error = RuntimeError("closed")
        error.partial_text = "Fictional partial"  # type: ignore[attr-defined]
        notice = codex_failure_notice(error)
        self.assertLess(notice.index("Ничего не повторяйте"), notice.index("<blockquote"))

    def test_refusal_detail_is_collapsed_and_escaped(self) -> None:
        notice = not_started_notice("Claude & Co", "claude_session_local_busy", "Busy <cli>.")
        self.assertIn("Claude &amp; Co не запущен", notice)
        self.assertIn("/return", notice)
        self.assertIn("<blockquote expandable>Busy &lt;cli&gt;.</blockquote>", notice)


if __name__ == "__main__":
    unittest.main()
