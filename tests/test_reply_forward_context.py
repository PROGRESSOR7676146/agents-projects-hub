from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.state import HubState
from hermes_codex_router.telegram import TopicMessage, parse_topic_message
from hermes_codex_router.telegram_quoted_context import (
    QUOTED_TEXT_LIMIT,
    bounded_payload,
    forward_origin_label,
    forwarded_text,
    reply_context,
    with_quoted_context,
)
from tests.fault_matrix_support import CHAT_ID, OWNER_ID, FaultMatrixHarness, RecordingAdapter

BOT = {"id": 7001, "is_bot": True, "username": "example_opencode_bot"}


def _message(update: dict[str, object]) -> dict[str, Any]:
    return cast(dict[str, Any], update["message"])


def _parsed(update: dict[str, object]) -> TopicMessage:
    parsed = parse_topic_message(update)
    assert parsed is not None
    return parsed


class _Updates:
    @staticmethod
    def update(message_id: int, thread_id: int, text: str) -> dict[str, object]:
        return {
            "update_id": message_id,
            "message": {
                "message_id": message_id,
                "message_thread_id": thread_id,
                "is_topic_message": True,
                "from": {"id": OWNER_ID, "is_bot": False},
                "chat": {"id": CHAT_ID, "type": "supergroup", "title": "Example"},
                "text": text,
            },
        }


class ReplyContextParsingTests(unittest.TestCase):
    harness = _Updates()

    def test_reply_to_bot_message_carries_author_and_text(self) -> None:
        update = self.harness.update(10, 951, "continue from this summary")
        _message(update)["reply_to_message"] = {
            "message_id": 9,
            "from": BOT,
            "text": "previous-session-summary-marker",
        }
        parsed = _parsed(update)
        assert parsed.reply_context is not None
        self.assertIn("From: the Telegram bot @example_opencode_bot", parsed.reply_context)
        self.assertIn("previous-session-summary-marker", parsed.reply_context)
        self.assertEqual(parsed.reply_to_username, "example_opencode_bot")

    def test_forum_topic_anchor_is_not_a_reply(self) -> None:
        update = self.harness.update(11, 951, "ordinary topic message")
        _message(update)["reply_to_message"] = {
            "message_id": 951,
            "from": {"id": OWNER_ID, "is_bot": False},
            "forum_topic_created": {"name": "Example"},
        }
        self.assertIsNone(_parsed(update).reply_context)

    def test_reply_to_own_photo_names_unattached_content(self) -> None:
        update = self.harness.update(12, 951, "what is on it")
        _message(update)["reply_to_message"] = {
            "message_id": 8,
            "from": {"id": OWNER_ID, "is_bot": False},
            "caption": "caption-marker",
            "photo": [{"file_id": "example-photo"}],
        }
        context = _parsed(update).reply_context
        assert context is not None
        self.assertIn("the user (their own earlier message)", context)
        self.assertIn("caption-marker", context)
        self.assertIn("a photo; that content is not attached", context)

    def test_reply_names_document_kind_without_file_name(self) -> None:
        update = self.harness.update(15, 951, "see the file")
        _message(update)["reply_to_message"] = {
            "message_id": 6,
            "from": BOT,
            "document": {"file_id": "example-doc", "file_name": "private-name-marker.pdf"},
        }
        context = _parsed(update).reply_context
        assert context is not None
        self.assertIn("a document", context)
        self.assertNotIn("private-name-marker", context)

    def test_reply_to_unknown_content_kind_says_it_is_not_attached(self) -> None:
        update = self.harness.update(16, 951, "what was it")
        _message(update)["reply_to_message"] = {
            "message_id": 5,
            "from": BOT,
            "checklist": {"title": "Example"},
        }
        context = _parsed(update).reply_context
        assert context is not None
        self.assertIn("any other content is not attached", context)

    def test_reply_text_is_bounded(self) -> None:
        update = self.harness.update(13, 951, "long")
        _message(update)["reply_to_message"] = {
            "message_id": 7,
            "from": BOT,
            "text": "x" * (QUOTED_TEXT_LIMIT + 50),
        }
        context = _parsed(update).reply_context
        assert context is not None
        self.assertIn("[...truncated]", context)
        self.assertLess(len(context), QUOTED_TEXT_LIMIT + 200)

    def test_external_reply_reports_unavailable_text(self) -> None:
        message = {
            "external_reply": {"origin": {"type": "channel", "chat": {"title": "Example News"}}}
        }
        context = reply_context(message, sender_id=OWNER_ID, topic_anchor=None)
        assert context is not None
        self.assertIn("the channel «Example News»", context)
        self.assertIn("not available to the bot", context)

    def test_forward_origin_labels_without_human_names(self) -> None:
        bot = {"forward_origin": {"type": "user", "sender_user": BOT}}
        person = {
            "forward_origin": {
                "type": "user",
                "sender_user": {"id": 99, "is_bot": False, "first_name": "Example Person"},
            }
        }
        hidden = {"forward_origin": {"type": "hidden_user", "sender_user_name": "Example"}}
        legacy = {"forward_date": 1}
        self.assertEqual(
            forward_origin_label(bot, sender_id=OWNER_ID),
            "the Telegram bot @example_opencode_bot",
        )
        self.assertEqual(forward_origin_label(person, sender_id=OWNER_ID), "another Telegram user")
        self.assertNotIn("Example", str(forward_origin_label(hidden, sender_id=OWNER_ID)))
        self.assertEqual(forward_origin_label(legacy, sender_id=OWNER_ID), "an unknown origin")
        self.assertIsNone(forward_origin_label({}, sender_id=OWNER_ID))
        self.assertEqual(
            forwarded_text(" body ", "the Telegram bot @example_opencode_bot"),
            "Forwarded from the Telegram bot @example_opencode_bot:\nbody",
        )
        longest = forwarded_text("y" * 4096, "the Telegram bot @example_opencode_bot")
        self.assertEqual(len(longest), QUOTED_TEXT_LIMIT)
        self.assertTrue(longest.endswith("[...truncated]"))

    def test_payload_keeps_current_message_and_reply_before_earlier_context(self) -> None:
        message = dataclasses.replace(
            _parsed(self.harness.update(17, 951, "x")),
            reply_context="reply-marker",
            quote_text=None,
        )
        earlier = "e" * 25_000
        payload = bounded_payload(
            f"{earlier}\n\nCURRENT USER MESSAGE:\nuse it",
            message,
            current_text_length=len("use it"),
            limit=20_000,
        )
        self.assertLessEqual(len(payload), 20_000)
        self.assertTrue(payload.startswith("[Earlier visible context was truncated"))
        self.assertIn("CURRENT USER MESSAGE:\nuse it", payload)
        self.assertTrue(payload.endswith("reply-marker"))
        plain = dataclasses.replace(message, reply_context=None)
        self.assertEqual(
            bounded_payload("short", plain, current_text_length=5, limit=20_000), "short"
        )
        self.assertNotIn(
            "context was truncated",
            bounded_payload("a" * 30_000, plain, current_text_length=10, limit=20_000)[-200:],
        )

    def test_context_is_shortened_before_user_text(self) -> None:
        message = dataclasses.replace(
            _parsed(self.harness.update(14, 951, "x")),
            reply_context="r" * 3000,
            quote_text="q" * 3000,
        )
        user_text = "u" * 15_000
        combined = with_quoted_context(user_text, message, limit=20_000)
        self.assertTrue(combined.startswith(user_text))
        self.assertLessEqual(len(combined), 20_000)
        self.assertIn("context was truncated", combined)


class ReplyForwardProviderInputTests(unittest.TestCase):
    def _harness(self, directory: str, *, quiet_ms: int = 0) -> FaultMatrixHarness:
        harness = FaultMatrixHarness(Path(directory))
        harness.config = dataclasses.replace(
            harness.config, message_batch_quiet_ms=quiet_ms, message_batch_max_ms=8000
        )
        return harness

    def _run_opencode(self, harness: FaultMatrixHarness) -> list[str]:
        adapter = RecordingAdapter("opencode")
        worker = harness.worker("opencode", adapter)
        sender = harness.sender()
        try:
            while worker.run_cycle():
                while sender._deliver_one("opencode"):
                    pass
        finally:
            sender.close()
            worker.close()
        return adapter.calls

    def test_plain_reply_text_reaches_the_replied_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = self._harness(directory)
            controller = harness.controller()
            update = harness.update(701, 971, "do what this summary says next")
            _message(update)["reply_to_message"] = {
                "message_id": 700,
                "from": BOT,
                "text": "summary-marker /new is quoted data",
            }
            try:
                self.assertTrue(controller.handle_update(update))
                topic = controller.state.find_topic(harness.chat_id, 971)
                assert topic is not None
                jobs = controller.state.provider_jobs_for_topic(topic.topic_id)
                self.assertEqual([job.agent_id for job in jobs], ["opencode"])
                controller.state.flush_message_batch(topic.topic_id)
            finally:
                controller.close()
            calls = self._run_opencode(harness)
            self.assertEqual(len(calls), 1)
            self.assertIn("REPLIED-TO TELEGRAM MESSAGE", calls[0])
            self.assertIn("summary-marker /new is quoted data", calls[0])
            self.assertIn("never routing", calls[0])

    def test_follow_up_part_keeps_its_selected_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = self._harness(directory, quiet_ms=5000)
            controller = harness.controller()
            select = harness.update(710, 972, "/agent opencode")
            first = harness.update(711, 972, "first part")
            second = harness.update(712, 972, "about this fragment")
            _message(second)["quote"] = {"text": "follow-up-quote-marker"}
            _message(second)["reply_to_message"] = {
                "message_id": 709,
                "from": BOT,
                "text": "whole message with follow-up-quote-marker inside",
            }
            try:
                for update in (select, first, second):
                    self.assertTrue(controller.handle_update(update))
                topic = controller.state.find_topic(harness.chat_id, 972)
                assert topic is not None
                jobs = controller.state.provider_jobs_for_topic(topic.topic_id)
                self.assertEqual(len(jobs), 1)
                controller.state.flush_message_batch(topic.topic_id)
            finally:
                controller.close()
            calls = self._run_opencode(harness)
            self.assertEqual(len(calls), 1)
            self.assertIn("SELECTED TELEGRAM QUOTE", calls[0])
            self.assertIn("whole message with follow-up-quote-marker inside", calls[0])

    def test_forward_after_comment_joins_the_collecting_turn_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = self._harness(directory, quiet_ms=5000)
            controller = harness.controller()
            select = harness.update(720, 973, "/agent opencode")
            comment = harness.update(721, 973, "please review what I forward")
            forward = harness.update(722, 973, "forwarded-body-marker")
            _message(forward)["forward_origin"] = {"type": "user", "sender_user": BOT}
            later = harness.update(723, 973, "a separate later request")
            try:
                for update in (select, comment, forward):
                    self.assertTrue(controller.handle_update(update))
                topic = controller.state.find_topic(harness.chat_id, 973)
                assert topic is not None
                self.assertEqual(len(controller.state.provider_jobs_for_topic(topic.topic_id)), 1)
                self.assertEqual(
                    controller.state.unseen_forwarded_context(topic.topic_id, "opencode"),
                    (None, None),
                )
                self.assertFalse(controller.handle_update(forward))
                controller.state.flush_message_batch(topic.topic_id)
                self.assertTrue(controller.handle_update(later))
                controller.state.flush_message_batch(topic.topic_id)
            finally:
                controller.close()
            calls = self._run_opencode(harness)
            self.assertEqual(len(calls), 2)
            self.assertIn("FORWARDED TELEGRAM MESSAGE", calls[0])
            self.assertIn(
                "Forwarded from the Telegram bot @example_opencode_bot:\nforwarded-body-marker",
                calls[0],
            )
            self.assertNotIn("forwarded-body-marker", calls[1])

    def test_forward_after_the_turn_closed_waits_for_the_next_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = self._harness(directory, quiet_ms=5000)
            controller = harness.controller()
            select = harness.update(730, 974, "/agent opencode")
            comment = harness.update(731, 974, "first request")
            forward = harness.update(732, 974, "late-forward-marker")
            _message(forward)["forward_origin"] = {"type": "hidden_user", "sender_user_name": "X"}
            later = harness.update(733, 974, "now use the forwarded text")
            try:
                for update in (select, comment):
                    self.assertTrue(controller.handle_update(update))
                topic = controller.state.find_topic(harness.chat_id, 974)
                assert topic is not None
                controller.state.flush_message_batch(topic.topic_id)
                self.assertTrue(controller.handle_update(forward))
                self.assertEqual(len(controller.state.provider_jobs_for_topic(topic.topic_id)), 1)
                self.assertTrue(controller.handle_update(later))
                controller.state.flush_message_batch(topic.topic_id)
            finally:
                controller.close()
            calls = self._run_opencode(harness)
            self.assertEqual(len(calls), 2)
            self.assertNotIn("late-forward-marker", calls[0])
            self.assertIn("late-forward-marker", calls[1])
            self.assertIn("a Telegram user who hides their account", calls[1])

    def test_longest_telegram_forward_is_stored_truncated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = self._harness(directory)
            controller = harness.controller()
            forward = harness.update(740, 975, "z" * 4096)
            _message(forward)["forward_origin"] = {"type": "user", "sender_user": BOT}
            try:
                self.assertTrue(controller.handle_update(forward))
                topic = controller.state.find_topic(harness.chat_id, 975)
                assert topic is not None
                context, _ = controller.state.unseen_forwarded_context(topic.topic_id, "codex")
                assert context is not None
                self.assertIn("[...truncated]", context)
            finally:
                controller.close()

    def test_passive_forward_backlog_keeps_the_reply_in_the_payload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = self._harness(directory)
            controller = harness.controller()
            updates = [harness.update(750, 976, "/agent opencode")]
            for index in range(6):
                forward = harness.update(751 + index, 976, f"backlog-{index} " + "f" * 3900)
                _message(forward)["forward_origin"] = {"type": "user", "sender_user": BOT}
                updates.append(forward)
            reply = harness.update(760, 976, "continue from this summary")
            _message(reply)["reply_to_message"] = {
                "message_id": 749,
                "from": BOT,
                "text": "backlog-summary-marker",
            }
            updates.append(reply)
            try:
                for update in updates:
                    self.assertTrue(controller.handle_update(update))
                topic = controller.state.find_topic(harness.chat_id, 976)
                assert topic is not None
                (job,) = controller.state.provider_jobs_for_topic(topic.topic_id)
            finally:
                controller.close()
            self.assertLessEqual(len(job.payload_text), 20_000)
            self.assertIn("backlog-summary-marker", job.payload_text)
            self.assertIn("continue from this summary", job.payload_text)
            self.assertIn("backlog-5", job.payload_text)

    def test_follow_up_after_an_overflowing_forward_starts_its_own_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = self._harness(directory, quiet_ms=5000)
            controller = harness.controller()
            select = harness.update(770, 977, "/agent opencode")
            comment = harness.update(771, 977, "c" * 17_000)
            forward = harness.update(772, 977, "overflow-forward-marker " + "o" * 3800)
            _message(forward)["forward_origin"] = {"type": "user", "sender_user": BOT}
            follow_up = harness.update(773, 977, "review it")
            try:
                for update in (select, comment, forward, follow_up):
                    self.assertTrue(controller.handle_update(update))
                topic = controller.state.find_topic(harness.chat_id, 977)
                assert topic is not None
                jobs = controller.state.provider_jobs_for_topic(topic.topic_id)
            finally:
                controller.close()
            self.assertEqual(len(jobs), 2)
            self.assertNotIn("overflow-forward-marker", jobs[0].payload_text)
            self.assertIn("overflow-forward-marker", jobs[1].payload_text)
            self.assertTrue(jobs[1].payload_text.rstrip().endswith("review it"))

    def test_forward_stays_passive_after_codex_selection_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = self._harness(directory, quiet_ms=5000)
            controller = harness.controller()
            comment = harness.update(780, 978, "codex request before a profile change")
            try:
                self.assertTrue(controller.handle_update(comment))
                topic = controller.state.find_topic(harness.chat_id, 978)
                assert topic is not None
                session = controller.state.active_session(topic.topic_id)
                assert session is not None
                (before,) = controller.state.provider_jobs_for_topic(topic.topic_id)
            finally:
                controller.close()
            other = HubState.open(
                harness.config.state_path, codex_permission_profile="example-other-policy"
            )
            try:
                self.assertTrue(
                    other.record_forwarded_quote(
                        topic_id=topic.topic_id,
                        chat_id=harness.chat_id,
                        message_id=781,
                        observer_agent_id="codex",
                        text="profile-forward-marker",
                        session=session,
                        batch_ms=(5000, 8000),
                        telegram_ingress_identity=None,
                    )
                )
                (after,) = other.provider_jobs_for_topic(topic.topic_id)
                context, _ = other.unseen_forwarded_context(topic.topic_id, "codex")
            finally:
                other.close()
            self.assertEqual(after.payload_text, before.payload_text)
            assert context is not None
            self.assertIn("profile-forward-marker", context)


if __name__ == "__main__":
    unittest.main()
