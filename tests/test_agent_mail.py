"""Agent mail between configured topics (ADR 0112); fictional Telegram only."""

from __future__ import annotations

import dataclasses
import hashlib
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.agent_letter_sender import AgentLetterSender
from hermes_codex_router.agent_letter_state import AgentLetterStore
from hermes_codex_router.agent_mail import (
    MAX_LETTER_BYTES,
    MailRoute,
    extract_letter,
    letter_address,
    letter_caption,
    parse_process_callback,
)
from hermes_codex_router.agent_mail_config import AgentMailConfigError, parse_agent_mail
from hermes_codex_router.artifacts import ValidatedArtifact
from hermes_codex_router.external_runtime import ExternalTurnResult
from hermes_codex_router.state import HubState
from hermes_codex_router.telegram import TelegramError, parse_topic_callback, parse_topic_message
from hermes_codex_router.telegram_interaction import telegram_user_turn_prompt
from tests.fault_matrix_support import CHAT_ID, OWNER_ID, FaultMatrixHarness, RecordingBot

SOURCE_CHAT = -1001111111111
TARGET_CHAT = -1002222222222
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)
ROUTE = MailRoute(
    alias="backup",
    source_project_id="example-hub",
    source_chat_id=SOURCE_CHAT,
    source_thread_id=7,
    target_project_id="example-backup",
    target_chat_id=TARGET_CHAT,
    target_thread_id=9,
)


class GrammarTests(unittest.TestCase):
    def test_valid_trailing_line_is_removed(self) -> None:
        extracted = extract_letter("Done.\n\n[[letter: backup-1a2b3c4d5e | TASK.md]]\n")
        self.assertEqual(extracted.text, "Done.")
        assert extracted.directive is not None
        self.assertEqual(extracted.directive.address, "backup-1a2b3c4d5e")
        self.assertEqual(extracted.directive.file_name, "TASK.md")
        self.assertFalse(extracted.malformed)

    def test_example_inside_open_fence_is_content(self) -> None:
        text = "Example:\n```\n[[letter: backup-1a2b3c4d5e | TASK.md]]"
        self.assertEqual(extract_letter(text).text, text)
        self.assertIsNone(extract_letter(text).directive)

    def test_line_that_is_not_last_is_ignored(self) -> None:
        text = "[[letter: backup-1a2b3c4d5e | TASK.md]]\nMore text."
        self.assertIsNone(extract_letter(text).directive)
        self.assertFalse(extract_letter(text).malformed)

    def test_malformed_lines_are_reported_and_kept(self) -> None:
        for line in (
            "[[letter: backup | TASK.md]]",
            "[[letter: backup-1a2b3c4d5e]]",
            "[[letter: backup-1a2b3c4d5e | ../TASK.md]]",
            "[[letter: backup-1a2b3c4d5e | a | b]]",
        ):
            with self.subTest(line=line):
                extracted = extract_letter(f"Answer\n{line}")
                self.assertTrue(extracted.malformed)
                self.assertEqual(extracted.text, f"Answer\n{line}")

    def test_text_that_only_resembles_a_letter_is_not_a_refusal(self) -> None:
        for line in ("[[letters are data]]", "[[letter backup-1a2b3c4d5e | TASK.md]]"):
            extracted = extract_letter(f"Answer\n{line}")
            self.assertFalse(extracted.malformed)
            self.assertIsNone(extracted.directive)

    def test_address_is_per_job_and_alias(self) -> None:
        first = letter_address("job-1", ROUTE)
        self.assertRegex(first, r"^backup-[0-9a-f]{10}$")
        self.assertEqual(first, letter_address("job-1", ROUTE))
        self.assertNotEqual(first, letter_address("job-2", ROUTE))
        retargeted = dataclasses.replace(ROUTE, target_thread_id=10)
        self.assertNotEqual(first, letter_address("job-1", retargeted))

    def test_caption_fits_telegram_and_escapes(self) -> None:
        caption = letter_caption(
            source_label="«example-hub / <Ops>»",
            agent_name="Claude",
            file_name="a&b.md",
            body="x" * 5000,
        )
        self.assertLessEqual(len(caption), 1024)
        self.assertIn("&lt;Ops&gt;", caption)
        self.assertIn("a&amp;b.md", caption)

    def test_callback_data_is_exact(self) -> None:
        letter_id = "0" * 32
        self.assertEqual(parse_process_callback(f"ml:{letter_id}"), letter_id)
        self.assertIsNone(parse_process_callback("ml:done"))
        self.assertIsNone(parse_process_callback(f"ml:{letter_id}x"))

    def test_contacts_precede_the_user_turn(self) -> None:
        prompt = telegram_user_turn_prompt("Hello", staging_dir="/tmp/x", contacts="AGENT MAIL")
        self.assertLess(prompt.index("AGENT MAIL"), prompt.index("CURRENT USER TURN"))
        self.assertNotIn("AGENT MAIL", telegram_user_turn_prompt("Hello"))


class ConfigTests(unittest.TestCase):
    chats = {"example-hub": SOURCE_CHAT, "example-backup": TARGET_CHAT}

    def route(self, **changes: Any) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "alias": "backup",
            "from": {"project_id": "example-hub", "chat_id": SOURCE_CHAT, "thread_id": 7},
            "to": {"project_id": "example-backup", "chat_id": TARGET_CHAT, "thread_id": 9},
        }
        raw.update(changes)
        return raw

    def parse(self, value: object, *, supported: bool = True) -> tuple[MailRoute, ...]:
        return parse_agent_mail(value, project_chats=self.chats, supported=supported)

    def test_absent_or_empty_keeps_mail_off(self) -> None:
        self.assertEqual(self.parse(None), ())
        self.assertEqual(self.parse({"routes": []}, supported=False), ())

    def test_valid_route(self) -> None:
        self.assertEqual(self.parse({"routes": [self.route()]}), (ROUTE,))

    def test_onboarded_project_is_accepted_and_checked_at_runtime(self) -> None:
        onboarded = {"project_id": "example-onboarded", "chat_id": -1009999999999, "thread_id": 1}
        (route,) = self.parse({"routes": [self.route(to=onboarded)]})
        self.assertEqual(route.target_project_id, "example-onboarded")
        borrowed = {"project_id": "example-onboarded", "chat_id": TARGET_CHAT, "thread_id": 1}
        with self.assertRaises(AgentMailConfigError):
            self.parse({"routes": [self.route(to=borrowed)]})

    def test_refusals(self) -> None:
        wrong_chat = {"project_id": "example-backup", "chat_id": SOURCE_CHAT, "thread_id": 9}
        same_topic = {"project_id": "example-hub", "chat_id": SOURCE_CHAT, "thread_id": 7}
        cases = {
            "unsupported": ({"routes": [self.route()]}, False),
            "extra key": ({"routes": [self.route()], "wake": True}, True),
            "bad alias": ({"routes": [self.route(alias="Backup!")]}, True),
            "foreign chat": ({"routes": [self.route(to=wrong_chat)]}, True),
            "same topic": ({"routes": [self.route(to=same_topic)]}, True),
            "duplicate": ({"routes": [self.route(), self.route()]}, True),
            "title": ({"routes": [self.route(to={"title": "Backups"})]}, True),
        }
        for name, (value, supported) in cases.items():
            with self.subTest(name), self.assertRaises(AgentMailConfigError):
                self.parse(value, supported=supported)


class MailStateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        root = self.base / "root"
        root.mkdir()
        self.state = HubState.open(self.base / "state.db", codex_permission_profile=None)
        self.addCleanup(self.state.close)
        self.source = self.state.observe_topic(
            project_id="example-hub",
            chat_id=SOURCE_CHAT,
            thread_id=7,
            title="Hub work",
            execution_root=root,
        )
        self.target = self.state.observe_topic(
            project_id="example-backup",
            chat_id=TARGET_CHAT,
            thread_id=9,
            title="Backups",
            execution_root=root,
        )
        self.session = self.state.activate_agent(
            self.source.topic_id, "claude", "claude-example", "high"
        )
        self.store = AgentLetterStore(self.state, agent_name="Claude")
        self.store.sync_routes((ROUTE,))
        self.messages = 0

    def leased_job(self):
        self.messages += 1
        self.state.enqueue_provider_job(
            idempotency_key=f"mail:{self.messages}",
            chat_id=SOURCE_CHAT,
            message_id=self.messages,
            topic_id=self.source.topic_id,
            agent_id="claude",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model=self.session.model,
            effort=self.session.effort,
            payload_text="Fictional request",
        )
        job = self.state.lease_provider_job("claude", "claude-worker")
        assert job is not None and job.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, job.lease_token)
        return job

    def artifact(self, name: str, content: bytes) -> ValidatedArtifact:
        path = self.base / f"spool-{hashlib.sha256(content).hexdigest()[:12]}-{name}"
        path.write_bytes(content)
        return ValidatedArtifact(
            path, name, len(content), "text/markdown", hashlib.sha256(content).hexdigest()
        )

    def answer(self, job: Any, file_name: str = "TASK.md") -> str:
        return f"Answer.\n[[letter: {letter_address(job.job_id, ROUTE)} | {file_name}]]"

    def commit(self, job: Any, draft: Any) -> None:
        self.state.commit_provider_result(
            job.job_id,
            job.lease_token,
            visible_response="Answer",
            sender_agent_id="claude",
            telegram_html="Answer",
            letter=draft,
            now=NOW,
        )

    def deliveries(self) -> list[sqlite3.Row]:
        return self.state._connection.execute(
            "SELECT * FROM agent_letter_deliveries ORDER BY kind"
        ).fetchall()


class PreparationTests(MailStateTestCase):
    def test_contacts_list_only_this_topics_routes(self) -> None:
        job = self.leased_job()
        prompt = self.store.contacts_prompt(job)
        self.assertIn(letter_address(job.job_id, ROUTE), prompt)
        self.assertIn("«example-backup / Backups»", prompt)
        self.assertIn("Обработать", prompt)
        self.store.sync_routes(())
        self.assertEqual(self.store.contacts_prompt(job), "")

    def test_valid_letter_is_prepared_and_committed_with_the_result(self) -> None:
        job = self.leased_job()
        outcome = self.store.prepare(
            job, self.answer(job), (self.artifact("TASK.md", "# Задача\nСделай".encode()),)
        )
        self.assertEqual(outcome.text, "Answer.")
        self.assertIn("поставлено в очередь", outcome.notice)
        assert outcome.draft is not None
        self.assertEqual(outcome.draft.body, "# Задача\nСделай")
        self.commit(job, outcome.draft)
        row = self.state._connection.execute("SELECT * FROM agent_letters").fetchone()
        self.assertEqual(row["source_job_id"], job.job_id)
        self.assertEqual(row["target_topic_id"], self.target.topic_id)
        (display,) = self.deliveries()
        self.assertEqual(
            (display["kind"], display["state"], display["chat_id"], display["thread_id"]),
            ("display", "pending", TARGET_CHAT, 9),
        )
        with self.assertRaises(sqlite3.DatabaseError):
            with self.state._connection:
                self.state._connection.execute("UPDATE agent_letters SET body='changed'")

    def test_refusals_keep_the_answer_and_explain(self) -> None:
        job = self.leased_job()
        good = self.artifact("TASK.md", b"text")
        cases = {
            "other address": (
                "Answer.\n[[letter: backup-0000000000 | TASK.md]]",
                (good,),
                "адрес не из списка",
            ),
            "missing file": (self.answer(job, "OTHER.md"), (good,), "не найден"),
            "wrong type": (self.answer(job, "TASK.pdf"), (good,), ".md или .txt"),
            "binary": (
                self.answer(job),
                (self.artifact("TASK.md", b"\xff\xfe\x00"),),
                "UTF-8",
            ),
            "too large": (
                self.answer(job),
                (self.artifact("TASK.md", b"a" * (MAX_LETTER_BYTES + 1)),),
                "UTF-8",
            ),
            "with choices": (
                "Answer.\n[[choices: A | B]]\n"
                f"[[letter: {letter_address(job.job_id, ROUTE)} | TASK.md]]",
                (good,),
                "кнопки",
            ),
            "malformed": ("Answer.\n[[letter: oops]]", (good,), "не распознана"),
        }
        for name, (text, artifacts, reason) in cases.items():
            with self.subTest(name):
                outcome = self.store.prepare(job, text, artifacts)
                self.assertIsNone(outcome.draft)
                self.assertIn("Письмо не отправлено", outcome.notice)
                self.assertIn(reason, outcome.notice)

    def test_ordinary_answer_is_untouched(self) -> None:
        job = self.leased_job()
        outcome = self.store.prepare(job, "Plain answer", ())
        self.assertEqual((outcome.text, outcome.notice, outcome.draft), ("Plain answer", "", None))

    def test_route_retargeted_during_the_turn_refuses_the_old_address(self) -> None:
        job = self.leased_job()
        offered = self.store.contacts_prompt(job)
        self.assertIn(letter_address(job.job_id, ROUTE), offered)
        other = self.state.observe_topic(
            project_id="example-backup",
            chat_id=TARGET_CHAT,
            thread_id=10,
            title="Elsewhere",
            execution_root=self.base / "root",
        )
        self.assertNotEqual(other.topic_id, self.target.topic_id)
        self.store.sync_routes((dataclasses.replace(ROUTE, target_thread_id=10),))
        outcome = self.store.prepare(job, self.answer(job), (self.artifact("TASK.md", b"text"),))
        self.assertIsNone(outcome.draft)
        self.assertIn("адрес не из списка", outcome.notice)

    def test_pathological_titles_never_abort_the_source_result(self) -> None:
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE topics SET title = ? WHERE topic_id = ?",
                ("&" * 128, self.source.topic_id),
            )
        job = self.leased_job()
        name = "&" * 60 + ".md"
        outcome = self.store.prepare(
            job,
            f"Answer.\n[[letter: {letter_address(job.job_id, ROUTE)} | {name}]]",
            (self.artifact(name, b"<&>" * 2000),),
        )
        assert outcome.draft is not None
        self.assertLessEqual(len(outcome.draft.caption_html), 1024)
        self.commit(job, outcome.draft)
        self.assertEqual(self.state.get_provider_result(job.job_id).visible_response, "Answer")

    def test_route_removed_before_commit_corrects_the_source(self) -> None:
        job = self.leased_job()
        outcome = self.store.prepare(job, self.answer(job), (self.artifact("TASK.md", b"text"),))
        self.store.sync_routes(())
        self.commit(job, outcome.draft)
        display, notice = self.deliveries()
        self.assertEqual((display["state"], display["error_code"]), ("rejected", "route_changed"))
        self.assertEqual(
            (notice["kind"], notice["state"], notice["chat_id"], notice["thread_id"]),
            ("failure_notice", "pending", SOURCE_CHAT, 7),
        )


class FakeLetterApi:
    def __init__(self) -> None:
        self.documents: list[dict[str, Any]] = []
        self.messages: list[tuple[int, int, str]] = []
        self.error: TelegramError | None = None
        self.document: dict[str, Any] = {
            "file_id": "file-example",
            "file_unique_id": "unique-example",
        }

    def send_document_bytes(
        self, chat_id: int, thread_id: int, content: bytes, **params: Any
    ) -> dict[str, Any]:
        if self.error is not None:
            raise self.error
        self.documents.append({"chat_id": chat_id, "thread_id": thread_id, "content": content})
        self.documents[-1].update(params)
        return {"message_id": 501, "document": self.document}

    def send_html(self, chat_id: int, thread_id: int, html: str, **params: Any) -> int:
        if self.error is not None:
            raise self.error
        self.messages.append((chat_id, thread_id, html))
        return 601


class SenderTests(MailStateTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.api = FakeLetterApi()
        self.events: list[str] = []
        self.failures: list[TelegramError] = []
        self.sender = AgentLetterSender(
            self.state,
            self.api,
            transport_success=lambda: None,
            transport_failure=self.failures.append,
            record_event=lambda level, code, detail: self.events.append(code),
            chat_registered=lambda chat_id, project_id: True,
        )
        job = self.leased_job()
        outcome = self.store.prepare(
            job, self.answer(job), (self.artifact("TASK.md", "Письмо".encode()),)
        )
        assert outcome.draft is not None
        self.letter_id = outcome.draft.letter_id
        self.commit(job, outcome.draft)

    def test_display_posts_the_document_with_the_button(self) -> None:
        self.assertTrue(self.sender.deliver_one(now=NOW))
        (document,) = self.api.documents
        self.assertEqual((document["chat_id"], document["thread_id"]), (TARGET_CHAT, 9))
        self.assertEqual(document["content"], "Письмо".encode())
        self.assertEqual(document["file_name"], "TASK.md")
        button = document["reply_markup"]["inline_keyboard"][0][0]
        self.assertEqual(button, {"text": "Обработать", "callback_data": f"ml:{self.letter_id}"})
        (display,) = self.deliveries()
        self.assertEqual(
            (display["state"], display["receipt_message_id"], display["receipt_file_id"]),
            ("confirmed", 501, "file-example"),
        )
        self.assertFalse(self.sender.deliver_one(now=NOW))

    def test_rate_limit_retries_later_without_a_notice(self) -> None:
        self.api.error = TelegramError(
            "slow down", failure_class="api_rejection", status_code=429, retry_after=7
        )
        self.sender.deliver_one(now=NOW)
        (display,) = self.deliveries()
        self.assertEqual((display["state"], display["attempts"]), ("pending", 1))
        self.assertFalse(self.sender.deliver_one(now=NOW + timedelta(seconds=6)))
        self.api.error = None
        self.assertTrue(self.sender.deliver_one(now=NOW + timedelta(seconds=8)))
        self.assertEqual(self.deliveries()[0]["state"], "confirmed")

    def test_rejection_is_final_and_tells_the_source_once(self) -> None:
        self.api.error = TelegramError(
            "topic closed", failure_class="api_rejection", status_code=400
        )
        self.sender.deliver_one(now=NOW)
        self.api.error = None
        self.assertTrue(self.sender.deliver_one(now=NOW))
        display, notice = self.deliveries()
        self.assertEqual((display["state"], display["error_code"]), ("rejected", "api_400"))
        self.assertEqual(notice["state"], "confirmed")
        ((chat_id, thread_id, text),) = self.api.messages
        self.assertEqual((chat_id, thread_id), (SOURCE_CHAT, 7))
        self.assertIn("отказал", text)
        self.assertFalse(self.sender.deliver_one(now=NOW))

    def test_ambiguous_send_is_unknown_and_never_repeated(self) -> None:
        self.api.error = TelegramError("timeout", failure_class="network_timeout")
        self.sender.deliver_one(now=NOW)
        self.api.error = None
        self.sender.deliver_one(now=NOW)
        display, notice = self.deliveries()
        self.assertEqual(display["state"], "unknown")
        self.assertEqual(notice["state"], "confirmed")
        self.assertIn("не получил подтверждения", self.api.messages[0][2])
        self.assertEqual(self.api.documents, [])

    def test_expired_lease_becomes_unknown(self) -> None:
        delivery = self.store.claim_next(now=NOW)
        assert delivery is not None
        self.assertEqual(self.store.recover_stale(now=NOW + timedelta(minutes=5)), 1)
        self.assertEqual(self.deliveries()[0]["state"], "unknown")
        self.store.confirm(
            delivery, message_id=9, file_id=None, file_unique_id=None, now=NOW
        )  # A late receipt after settlement changes nothing.
        self.assertEqual(self.deliveries()[0]["state"], "unknown")

    def test_route_removed_before_display_is_rejected_and_settled_without_routes(self) -> None:
        self.store.sync_routes(())
        self.assertTrue(self.sender.deliver_one(now=NOW))
        self.assertTrue(self.sender.deliver_one(now=NOW))
        self.assertEqual(self.api.documents, [])
        display, notice = self.deliveries()
        self.assertEqual((display["state"], display["error_code"]), ("rejected", "route_removed"))
        self.assertEqual((notice["state"], notice["chat_id"]), ("confirmed", SOURCE_CHAT))

    def test_incomplete_receipt_is_unknown_and_never_repeated(self) -> None:
        self.api.document = {}
        self.sender.deliver_one(now=NOW)
        self.sender.deliver_one(now=NOW)
        display, notice = self.deliveries()
        self.assertEqual(
            (display["state"], display["error_code"], display["receipt_message_id"]),
            ("unknown", "incomplete_receipt", 501),
        )
        self.assertEqual(notice["state"], "confirmed")
        self.assertEqual(len(self.api.documents), 1)
        self.assertIsNone(
            self.store.displayed_letter(
                self.letter_id, chat_id=TARGET_CHAT, thread_id=9, message_id=501
            )
        )

    def test_failure_notice_is_withheld_from_a_rebound_source(self) -> None:
        self.api.error = TelegramError("gone", failure_class="api_rejection", status_code=400)
        self.sender.deliver_one(now=NOW)
        self.api.error = None
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE topics SET project_id = 'example-other' WHERE topic_id = ?",
                (self.source.topic_id,),
            )
        self.sender.deliver_one(now=NOW)
        self.assertEqual(self.api.messages, [])
        self.assertEqual(self.deliveries()[1]["error_code"], "topic_unbound")

    def test_unbound_target_is_rejected_before_sending(self) -> None:
        self.sender._chat_registered = lambda chat_id, project_id: False
        self.sender.deliver_one(now=NOW)
        self.assertEqual(self.api.documents, [])
        self.assertEqual(self.deliveries()[0]["error_code"], "topic_unbound")

    def test_rebound_target_cannot_process_an_old_display(self) -> None:
        self.sender.deliver_one(now=NOW)
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE topics SET project_id = 'example-other' WHERE topic_id = ?",
                (self.target.topic_id,),
            )
        self.assertIsNone(
            self.store.displayed_letter(
                self.letter_id, chat_id=TARGET_CHAT, thread_id=9, message_id=501
            )
        )

    def test_handled_but_refused_press_spends_the_button_without_claiming_delivery(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from hermes_codex_router.agent_letter_callback import handle_letter_process
        from hermes_codex_router.telegram import TopicCallback

        self.sender.deliver_one(now=NOW)
        calls: list[tuple[str, dict[str, object]]] = []
        answers: list[str] = []
        telegram = SimpleNamespace(
            call=lambda method, **params: calls.append((method, params)),
            answer_callback=lambda callback_id, text="": answers.append(text),
        )
        service = SimpleNamespace(
            config=SimpleNamespace(
                agent_mail=(ROUTE,), hub_bot=SimpleNamespace(telegram_user_id=9001)
            ),
            state=self.state,
            telegram=telegram,
            ingress_identity="hub",
            agent=SimpleNamespace(agent_id="codex"),
            handle_update=lambda update: True,  # Handled, e.g. refused for a local writer.
        )
        callback = TopicCallback(
            callback_id="press",
            message_id=501,
            chat_id=TARGET_CHAT,
            thread_id=9,
            sender_id=42,
            data=f"ml:{self.letter_id}",
            message_sender_id=9001,
        )
        with mock.patch(
            "hermes_codex_router.agent_letter_callback._project_current", return_value=True
        ):
            self.assertTrue(handle_letter_process(service, callback))
            self.assertTrue(handle_letter_process(service, callback))
        (edit,) = [params for method, params in calls if method == "editMessageReplyMarkup"]
        self.assertIn("Не передано", str(edit["reply_markup"]))
        self.assertEqual(
            answers, ["Hub не принял письмо, причина — в теме", "Письмо уже передано агенту"]
        )

    def test_displayed_letter_matches_only_the_exact_message(self) -> None:
        self.sender.deliver_one(now=NOW)
        found = self.store.displayed_letter(
            self.letter_id, chat_id=TARGET_CHAT, thread_id=9, message_id=501
        )
        assert found is not None
        self.assertEqual((found.file_id, found.file_name), ("file-example", "TASK.md"))
        for chat_id, thread_id, message_id in (
            (TARGET_CHAT, 9, 502),
            (TARGET_CHAT, 8, 501),
            (SOURCE_CHAT, 9, 501),
        ):
            self.assertIsNone(
                self.store.displayed_letter(
                    self.letter_id, chat_id=chat_id, thread_id=thread_id, message_id=message_id
                )
            )


class SyntheticProcessMessageTests(unittest.TestCase):
    def test_document_and_caption_parse_as_ordinary_owner_input(self) -> None:
        from hermes_codex_router.telegram import _InternalSyntheticTopicUpdate

        parsed = parse_topic_message(
            _InternalSyntheticTopicUpdate(
                update_id=-5,
                message={
                    "message_id": 501,
                    "message_thread_id": 9,
                    "is_topic_message": True,
                    "from": {"id": 42, "is_bot": False},
                    "chat": {"id": TARGET_CHAT, "type": "supergroup", "is_forum": True},
                    "caption": "Обработай письмо",
                    "document": {
                        "file_id": "file-example",
                        "file_unique_id": "unique-example",
                        "file_name": "TASK.md",
                        "mime_type": "text/markdown",
                        "file_size": 12,
                    },
                },
            )
        )
        assert parsed is not None
        self.assertEqual(
            (parsed.text, parsed.thread_id, parsed.sender_id), ("Обработай письмо", 9, 42)
        )
        (attachment,) = parsed.attachments
        self.assertEqual((attachment.kind, attachment.file_id), ("document", "file-example"))
        self.assertFalse(parsed.is_forwarded)


HUB_BOT_ID = 9001


class LetterWritingAdapter:
    """Answers once with a staged letter addressed to the first offered contact."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def run_turn(self, **kwargs: object) -> ExternalTurnResult:
        prompt = str(kwargs["prompt"])
        self.prompts.append(prompt)
        match = re.search(r"^- (backup-[0-9a-f]{10}): ", prompt, re.MULTILINE)
        if match is None:
            return ExternalTurnResult("opencode", "Processed the letter.", "session-2", "model")
        staging = Path(str(kwargs["staging_dir"]))
        (staging / "TASK.md").write_text("# Задача\nПерепинить бэкап.\n", encoding="utf-8")
        text = f"Пишу письмо.\n[[letter: {match.group(1)} | TASK.md]]"
        return ExternalTurnResult("opencode", text, "session-1", "model")


class HubLetterBot(RecordingBot):
    def __init__(self) -> None:
        super().__init__()
        self.documents: list[dict[str, Any]] = []

    def send_document_bytes(
        self, chat_id: int, thread_id: int, content: bytes, **params: Any
    ) -> dict[str, Any]:
        self.documents.append({"chat_id": chat_id, "thread_id": thread_id, "content": content})
        self.documents[-1].update(params)
        return {
            "message_id": 8801,
            "document": {"file_id": "letter-file", "file_unique_id": "letter-unique"},
        }


class ControllerBot(RecordingBot):
    def __init__(self, files: dict[str, bytes]) -> None:
        super().__init__()
        self.files = files
        self.answers: list[str] = []
        self.calls: list[str] = []

    def download_file(self, file_id: str, destination: Path, *, max_bytes: int):
        from hermes_codex_router.telegram import DownloadedTelegramFile

        content = self.files[file_id]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        return DownloadedTelegramFile(
            destination, len(content), hashlib.sha256(content).hexdigest()
        )

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.answers.append(text)

    def call(self, method: str, **params: object) -> object:
        self.calls.append(method)
        return True


class EndToEndTests(unittest.TestCase):
    def test_letter_travels_from_source_answer_to_owner_processed_target_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = FaultMatrixHarness(Path(directory))
            route = MailRoute(
                "backup", "example-project", CHAT_ID, 951, "example-project", CHAT_ID, 952
            )
            assert harness.config.hub_bot is not None
            harness.config = dataclasses.replace(
                harness.config,
                agent_mail=(route,),
                hub_bot=dataclasses.replace(harness.config.hub_bot, telegram_user_id=HUB_BOT_ID),
            )
            controller = harness.controller()
            try:
                AgentLetterStore(controller.state).sync_routes(harness.config.agent_mail)
                for message_id, thread_id, text in (
                    (1, 952, "/agent opencode"),
                    (2, 951, "/agent opencode"),
                    (3, 951, "напиши письмо агенту бэкапа"),
                ):
                    self.assertTrue(
                        controller.handle_update(harness.update(message_id, thread_id, text))
                    )
            finally:
                controller.close()

            adapter = LetterWritingAdapter()
            hub = HubLetterBot()
            source_bot = RecordingBot()
            worker = harness.worker("opencode", adapter)
            sender = harness.sender(hub=hub, opencode=source_bot)
            try:
                self.assertTrue(worker.run_cycle())
                while sender._deliver_one("opencode"):
                    pass
                self.assertTrue(sender.letters is not None and sender.letters.deliver_one())
            finally:
                sender.close()
                worker.close()

            self.assertIn("AGENT MAIL FOR THIS TURN", adapter.prompts[0])
            (answer,) = [html for _chat, _thread, html in source_bot.sent if "Пишу письмо" in html]
            self.assertNotIn("[[letter", answer)
            self.assertIn("поставлено в очередь", answer)
            (document,) = hub.documents
            self.assertEqual((document["chat_id"], document["thread_id"]), (CHAT_ID, 952))
            letter_id = document["reply_markup"]["inline_keyboard"][0][0]["callback_data"][3:]

            controller = harness.controller()
            bot = ControllerBot({"letter-file": document["content"]})
            cast(Any, controller).telegram = bot
            try:

                def press(callback_id: str, sender_id: int = OWNER_ID) -> bool:
                    callback = parse_topic_callback(
                        {
                            "update_id": 50,
                            "callback_query": {
                                "id": callback_id,
                                "from": {"id": sender_id, "is_bot": False},
                                "data": f"ml:{letter_id}",
                                "message": {
                                    "message_id": 8801,
                                    "message_thread_id": 952,
                                    "is_topic_message": True,
                                    "from": {"id": HUB_BOT_ID, "is_bot": True},
                                    "chat": {"id": CHAT_ID, "type": "supergroup"},
                                },
                            },
                        }
                    )
                    assert callback is not None
                    return cast(Any, controller)._handle_callback(callback)

                self.assertFalse(press("press-stranger", sender_id=777))
                target = controller.state.find_topic(CHAT_ID, 952)
                assert target is not None
                self.assertEqual(controller.state.provider_jobs_for_topic(target.topic_id), ())
                self.assertTrue(press("press-1"))
                self.assertTrue(press("press-2"))
                (job,) = controller.state.provider_jobs_for_topic(target.topic_id)
                self.assertIn("Обработай письмо агента", job.payload_text)
                (material,) = controller.state.incoming_materials_for_job(job.job_id)
                self.assertEqual(Path(str(material.storage_path)).read_bytes(), document["content"])
                self.assertEqual(bot.answers[-1], "Письмо уже передано агенту")
                self.assertIn("editMessageReplyMarkup", bot.calls)
            finally:
                controller.close()

            worker = harness.worker("opencode", adapter)
            try:
                self.assertTrue(worker.run_cycle())
            finally:
                worker.close()
            self.assertIn("Обработай письмо агента", adapter.prompts[1])
            self.assertIn("Перепинить бэкап.", adapter.prompts[1])
            self.assertNotIn("AGENT MAIL FOR THIS TURN", adapter.prompts[1])


if __name__ == "__main__":
    unittest.main()
