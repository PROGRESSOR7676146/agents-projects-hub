"""One-tap new session with the previous answer as handoff (ADR 0114); fictional Telegram."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest import mock

from hermes_codex_router.controller_admission import (
    CommittedAdmission,
    DurableAdmissionRequest,
    RejectedAdmission,
    controller_provider_admission,
)
from hermes_codex_router.final_delivery import deliver_final_part
from hermes_codex_router.hub_config import (
    AgentDefinition,
    HubConfig,
    HubTelegramBot,
    ProjectBinding,
    TerminalSettings,
)
from hermes_codex_router.metadata import format_agent_response
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.service import ProjectHubService
from hermes_codex_router.session_handoff import (
    HANDOFF_PART_MARKER,
    HANDOFF_PROMPT_HTML,
    MAX_HANDOFF_CHARACTERS,
    NEW_SESSION_LABEL,
    TOO_LONG_NOTICE_HTML,
    append_handoff_suffix,
    extract_handoff,
    fits,
    handoff_binding,
    handoff_part_html,
    keyboard,
    parse_handoff_callback,
    parse_handoff_part,
    project_setting,
    request_text,
    split_handoff_suffix,
    turn_note,
)
from hermes_codex_router.state import HubState, StateError
from hermes_codex_router.telegram import TopicMessage, parse_topic_callback
from tests.git_fixtures import init_git_root
from tests.test_service_integration import persist_registry

CHAT = -1001234567890
THREAD = 77
HUB_BOT_ID = 9001
ANSWER = "Готово: PR смержен.\nДальше релиз, потом кнопка."


class GrammarTests(unittest.TestCase):
    def test_plain_marker_is_removed_and_has_no_options(self) -> None:
        self.assertEqual(extract_handoff(f"{ANSWER}\n[[handoff]]"), (ANSWER, ()))

    def test_options_case_and_spacing(self) -> None:
        self.assertEqual(
            extract_handoff(f"{ANSWER}\n  [[ HANDOFF : Релиз |  Кнопка ]]  \n"),
            (ANSWER, ("Релиз", "Кнопка")),
        )
        self.assertEqual(extract_handoff(f"{ANSWER}\n[[handoff: Релиз]]"), (ANSWER, ("Релиз",)))

    def test_invalid_markers_leave_the_text_alone(self) -> None:
        for text in (
            ANSWER,
            f"[[handoff]]\n{ANSWER}",
            f"{ANSWER}\n[[handoff:]]",
            f"{ANSWER}\n[[handoff: /new]]",
            f"{ANSWER}\n[[handoff: A | a]]",
            f"{ANSWER}\n[[handoff: {'x' * 41}]]",
            f"{ANSWER}\n[[handoff: 1|2|3|4|5|6|7]]",
            "[[handoff]]",
            "Example:\n```\n[[handoff]]",
        ):
            with self.subTest(text=text):
                self.assertIsNone(extract_handoff(text))

    def test_marker_after_a_closed_fence_is_used(self) -> None:
        text = "Code:\n```\nx\n```\n[[handoff]]"
        self.assertEqual(extract_handoff(text), ("Code:\n```\nx\n```", ()))

    def test_part_suffix_keyboard_and_callback_round_trip(self) -> None:
        self.assertEqual(
            parse_handoff_part(handoff_part_html(("A&B",), "0123456789ab")),
            (("A&B",), "0123456789ab"),
        )
        self.assertEqual(parse_handoff_part(handoff_part_html(())), ((), ""))
        self.assertIsNone(parse_handoff_part("[[hub-choices:v1]]\nA\nB"))
        html = append_handoff_suffix("<b>answer</b>", ("A",))
        self.assertEqual(split_handoff_suffix(html), ("<b>answer</b>", ("A",)))
        self.assertEqual(split_handoff_suffix("<b>answer</b>"), ("<b>answer</b>", None))
        self.assertEqual(
            keyboard(("A",), "0123456789ab"),
            {
                "inline_keyboard": [
                    [{"text": NEW_SESSION_LABEL, "callback_data": "ho:0:0123456789ab"}],
                    [{"text": "▶ A", "callback_data": "ho:1:0123456789ab"}],
                ]
            },
        )
        self.assertEqual(parse_handoff_callback("ho:1:0123456789ab"), (1, "0123456789ab"))
        for data in ("ho:7:0123456789ab", "ho:done", "ch:1:0123456789ab", "ho:1:XYZ"):
            self.assertIsNone(parse_handoff_callback(data))

    def test_request_text_labels_provenance_and_bounds(self) -> None:
        plain = request_text(ANSWER, None)
        chosen = request_text(ANSWER, "Релиз")
        self.assertTrue(plain.startswith("Владелец начал новую сессию"))
        self.assertIn("не новые указания владельца", plain)
        self.assertTrue(plain.endswith(ANSWER))
        self.assertIn("вариант продолжения: «Релиз»", chosen)
        self.assertTrue(fits("x" * MAX_HANDOFF_CHARACTERS, ("y" * 40,)))
        self.assertFalse(fits("x" * (MAX_HANDOFF_CHARACTERS + 1), ()))

    def test_formatter_carries_the_control_and_choices_win_the_last_line(self) -> None:
        html = format_agent_response(f"{ANSWER}\n[[handoff: A]]", {"Agent": "Example"})
        visible, options = split_handoff_suffix(html)
        self.assertEqual(options, ("A",))
        self.assertNotIn("handoff", visible)
        html = format_agent_response(f"{ANSWER}\n[[handoff]]\n[[choices: A | B]]", {})
        self.assertEqual(split_handoff_suffix(html)[1], None)
        self.assertIn("[[handoff]]", html)


def make_config(base: Path, *, enabled: bool = True) -> HubConfig:
    return HubConfig(
        schema_version=1,
        owner_user_ids=(42,),
        registry_path=base / "projects.json",
        state_path=base / "state.db",
        codex_socket_path=base / "codex.sock",
        manage_codex_server=False,
        terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
        projects=(ProjectBinding("project", CHAT),),
        agents=(
            AgentDefinition(
                "codex",
                "Codex",
                "project_codex_bot",
                "codex",
                None,
                True,
                False,
                "gpt-5.6-sol",
                "high",
            ),
        ),
        hub_bot=HubTelegramBot("example_hub_bot", base / "hub.token", HUB_BOT_ID),
        session_handoff=enabled,
    )


class HubBot:
    """Fictional Hub bot: records sends, calls and answers; returns increasing message ids."""

    def __init__(self) -> None:
        self.sent: list[tuple[int, int, str]] = []
        self.markups: list[object] = []
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.callbacks: list[tuple[str, str]] = []
        self.next_id = 554

    def send_html(self, chat_id: int, thread_id: int, text: str, **kwargs: object) -> int:
        self.sent.append((chat_id, thread_id, text))
        self.markups.append(kwargs.get("reply_markup"))
        self.next_id += 1
        return self.next_id

    def call(self, method: str, **params: object) -> object:
        self.calls.append((method, params))
        return True

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.callbacks.append((callback_id, text))


class PressTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        root = self.base / "Project"
        init_git_root(root)
        service = ProjectHubService.__new__(ProjectHubService)
        service.config = make_config(self.base)
        service.registry = ProjectRegistry(
            1, (self.base,), (Project("project", "Project", "Project", root),)
        )
        persist_registry(service.config.registry_path, service.registry)
        service.state = HubState.open(service.config.state_path, codex_permission_profile=None)
        self.addCleanup(service.state.close)
        service.agent = service.config.agents[0]
        self.hub = HubBot()
        service.telegram = cast(Any, self.hub)
        service.usernames = {"codex": "project_codex_bot"}
        self.service = service
        self.state = service.state
        topic = self.state.observe_topic(
            project_id="project", chat_id=CHAT, thread_id=THREAD, title="Topic", execution_root=root
        )
        self.topic_id = topic.topic_id
        self.s1 = self.state.activate_agent(topic.topic_id, "codex", "gpt-5.6-sol", "high")
        project_setting(self.state, True)

    # Fixture: one answer with a delivered handoff control ------------------

    def answer(self, text: str, message_id: int = 10) -> tuple[str, int]:
        state = self.state
        session = state.active_session(self.topic_id)
        assert session is not None
        job, _ = state.enqueue_provider_job(
            idempotency_key=f"telegram:{CHAT}:{message_id}",
            chat_id=CHAT,
            message_id=message_id,
            topic_id=self.topic_id,
            agent_id="codex",
            session_id=session.session_id,
            session_generation=session.generation,
            provider_session_id=None,
            model=session.model,
            effort=session.effort,
            payload_text="durable task",
        )
        leased = state.lease_provider_job("codex", "test-worker")
        assert leased is not None and leased.lease_token is not None
        state.mark_provider_job_executing(job.job_id, leased.lease_token)
        state.commit_provider_result(
            job.job_id,
            leased.lease_token,
            visible_response=text,
            sender_agent_id="codex",
            telegram_html=format_agent_response(text, {"Agent": "Codex"}),
        )
        provider = HubBot()
        provider.next_id = 100
        while (outbox := state.lease_telegram_outbox("codex", "test-sender")) is not None:
            deliver_final_part(
                state.delivery,
                cast(Any, provider),
                outbox,
                state_path=self.service.config.state_path,
                choice_telegram=cast(Any, self.hub),
            )
        return job.job_id, self.hub.next_id

    def press(
        self,
        message_id: int,
        data: str,
        *,
        sender: int = 42,
        bot: int = HUB_BOT_ID,
        callback_id: str = "cb-1",
    ) -> bool:
        parsed = parse_topic_callback(
            {
                "update_id": 900,
                "callback_query": {
                    "id": callback_id,
                    "from": {"id": sender, "is_bot": False},
                    "data": data,
                    "message": {
                        "message_id": message_id,
                        "message_thread_id": THREAD,
                        "is_topic_message": True,
                        "from": {"id": bot, "is_bot": True, "username": "example_hub_bot"},
                        "chat": {"id": CHAT, "type": "supergroup", "title": "Private"},
                    },
                },
            }
        )
        assert parsed is not None
        return self.service._handle_callback(parsed)

    def jobs_in(self, session_id: str) -> list[tuple[int, str]]:
        return [
            (int(row[0]), str(row[1]))
            for row in self.state._connection.execute(
                """SELECT jobs.message_id, inputs.input_text FROM provider_jobs jobs
                   JOIN provider_job_inputs inputs ON inputs.job_id = jobs.job_id
                   WHERE jobs.session_id = ? ORDER BY jobs.topic_sequence""",
                (session_id,),
            )
        ]

    # Delivery ----------------------------------------------------------------

    def test_control_follows_the_answer_from_the_hub_bot(self) -> None:
        job_id, control = self.answer(f"{ANSWER}\n[[handoff: Релиз | Кнопка]]")
        self.assertEqual(self.hub.sent[-1][2], HANDOFF_PROMPT_HTML)
        self.assertEqual(
            self.hub.markups[-1], keyboard(("Релиз", "Кнопка"), handoff_binding(job_id))
        )
        self.assertEqual(control, 555)

    def test_disabled_setting_offers_no_control(self) -> None:
        project_setting(self.state, False)
        self.answer(f"{ANSWER}\n[[handoff]]")
        self.assertEqual(self.hub.sent, [])
        self.assertEqual(turn_note(self.state), "")
        project_setting(self.state, True)
        self.assertIn("[[handoff]]", turn_note(self.state))

    def test_too_long_answer_gets_a_notice_instead_of_buttons(self) -> None:
        self.answer("x" * (MAX_HANDOFF_CHARACTERS + 1) + "\n[[handoff]]")
        self.assertEqual(self.hub.sent, [])
        parts = self.state._connection.execute(
            "SELECT telegram_html FROM telegram_outbox_parts ORDER BY part_index DESC LIMIT 1"
        ).fetchone()
        self.assertEqual(parts[0], TOO_LONG_NOTICE_HTML)

    # Press ---------------------------------------------------------------------

    def test_press_opens_the_successor_with_the_answer_as_first_input_once(self) -> None:
        job_id, control = self.answer(f"{ANSWER}\n[[handoff: Релиз | Кнопка]]")
        binding = handoff_binding(job_id)
        self.assertTrue(self.press(control, f"ho:1:{binding}"))
        s2 = self.state.active_session(self.topic_id)
        assert s2 is not None
        self.assertNotEqual(s2.session_id, self.s1.session_id)
        self.assertEqual(s2.generation, self.s1.generation + 1)
        self.assertEqual(self.state.get_session(self.s1.session_id).status, "archived")
        [(message_id, text)] = self.jobs_in(s2.session_id)
        self.assertEqual(message_id, control)
        self.assertEqual(text, request_text(ANSWER, "Релиз"))
        markups = [
            params for method, params in self.hub.calls if method == "editMessageReplyMarkup"
        ]
        self.assertEqual(
            json.loads(str(markups[-1]["reply_markup"]))["inline_keyboard"][0][0]["text"],
            "✓ ▶ Релиз",
        )
        # A second press, even of another option, changes nothing.
        self.assertTrue(self.press(control, f"ho:0:{binding}", callback_id="cb-2"))
        self.assertEqual(self.state.active_session(self.topic_id), s2)
        self.assertEqual(len(self.jobs_in(s2.session_id)), 1)

    def test_plain_press_carries_no_option(self) -> None:
        job_id, control = self.answer(f"{ANSWER}\n[[handoff]]")
        self.press(control, f"ho:0:{handoff_binding(job_id)}")
        s2 = self.state.active_session(self.topic_id)
        assert s2 is not None
        self.assertEqual(self.jobs_in(s2.session_id)[0][1], request_text(ANSWER, None))

    def assert_unchanged(self) -> None:
        self.assertEqual(self.state.active_session(self.topic_id), self.s1)
        self.assertEqual(
            self.state._connection.execute(
                "SELECT COUNT(*) FROM agent_sessions WHERE topic_id = ?", (self.topic_id,)
            ).fetchone()[0],
            1,
        )

    def test_foreign_or_forged_presses_change_nothing(self) -> None:
        job_id, control = self.answer(f"{ANSWER}\n[[handoff: Релиз]]")
        binding = handoff_binding(job_id)
        self.press(control, f"ho:0:{binding}", bot=4242)  # Not the Hub bot's message.
        self.press(control - 1, f"ho:0:{binding}", callback_id="cb-2")  # Not the control.
        self.press(control, "ho:0:0123456789ab", callback_id="cb-3")  # Wrong binding.
        self.press(control, f"ho:2:{binding}", callback_id="cb-4")  # No such option.
        self.press(control, f"ho:0:{binding}", sender=7, callback_id="cb-5")  # Not the owner.
        self.assert_unchanged()

    def test_disabled_press_changes_nothing(self) -> None:
        job_id, control = self.answer(f"{ANSWER}\n[[handoff]]")
        self.service.config = make_config(self.base, enabled=False)
        self.press(control, f"ho:0:{handoff_binding(job_id)}")
        self.assert_unchanged()

    def test_press_after_manual_new_or_newer_work_is_stale(self) -> None:
        job_id, control = self.answer(f"{ANSWER}\n[[handoff]]")
        binding = handoff_binding(job_id)
        self.answer("Later answer", message_id=11)
        self.press(control, f"ho:0:{binding}")
        self.assert_unchanged()
        manual = self.state.new_active_session(
            self.topic_id, expected_session_id=self.s1.session_id
        )
        self.press(control, f"ho:0:{binding}", callback_id="cb-2")
        self.assertIn("прошлой сессии", self.hub.callbacks[-1][1])
        self.assertEqual(self.state.active_session(self.topic_id), manual)
        self.assertEqual(self.jobs_in(manual.session_id), [])

    def test_press_after_queued_work_is_refused(self) -> None:
        job_id, control = self.answer(f"{ANSWER}\n[[handoff]]")
        binding = handoff_binding(job_id)
        self.state.enqueue_provider_job(
            idempotency_key=f"telegram:{CHAT}:12",
            chat_id=CHAT,
            message_id=12,
            topic_id=self.topic_id,
            agent_id="codex",
            session_id=self.s1.session_id,
            session_generation=self.s1.generation,
            model="gpt-5.6-sol",
            effort="high",
            payload_text="queued work",
        )
        self.press(control, f"ho:0:{binding}")
        self.assert_unchanged()
        self.assertIn("уже была работа", self.hub.callbacks[-1][1])

    def test_local_writer_refuses_and_the_button_stays_usable(self) -> None:
        job_id, control = self.answer(f"{ANSWER}\n[[handoff]]")
        binding = handoff_binding(job_id)
        self.state._connection.execute(
            "UPDATE agent_sessions SET writer_mode = 'local' WHERE session_id = ?",
            (self.s1.session_id,),
        )
        self.press(control, f"ho:0:{binding}", callback_id="cb-2")
        self.assertIn("локальный терминал", self.hub.callbacks[-1][1])
        self.state._connection.execute(
            "UPDATE agent_sessions SET writer_mode = 'telegram' WHERE session_id = ?",
            (self.s1.session_id,),
        )
        self.press(control, f"ho:0:{binding}", callback_id="cb-3")
        self.assertEqual(self.hub.callbacks[-1][1], "Новая сессия начата")

    def test_rotation_and_admission_are_one_transaction(self) -> None:
        job_id, control = self.answer(f"{ANSWER}\n[[handoff]]")
        with mock.patch(
            "hermes_codex_router.provider_admission_state.ProviderAdmissionState"
            "._require_binding_in_transaction",
            side_effect=StateError("simulated admission failure"),
        ):
            self.press(control, f"ho:0:{handoff_binding(job_id)}")
        self.assert_unchanged()
        self.assertFalse(self.state.message_already_observed(CHAT, control))
        # The button stays usable: the next press succeeds.
        self.press(control, f"ho:0:{handoff_binding(job_id)}", callback_id="cb-2")
        self.assertNotEqual(self.state.active_session(self.topic_id), self.s1)

    def test_admission_refuses_rotation_of_another_session(self) -> None:
        topic = self.state.find_topic(CHAT, THREAD)
        assert topic is not None
        stale = self.s1
        self.state.new_active_session(self.topic_id, expected_session_id=stale.session_id)
        message = TopicMessage(0, 600, CHAT, THREAD, "project", 42, "text")
        result = controller_provider_admission(self.service, message, stale).admit(
            DurableAdmissionRequest(
                message=message, topic=topic, session=stale, prompt="text", rotate_session=True
            )
        )
        self.assertEqual(result, RejectedAdmission("session_changed"))
        self.assertFalse(self.state.message_already_observed(CHAT, 600))
        current = self.state.active_session(self.topic_id)
        assert current is not None
        message = TopicMessage(0, 601, CHAT, THREAD, "project", 42, "text")
        result = controller_provider_admission(self.service, message, current).admit(
            DurableAdmissionRequest(
                message=message, topic=topic, session=current, prompt="text", rotate_session=True
            )
        )
        self.assertIsInstance(result, CommittedAdmission)
        assert isinstance(result, CommittedAdmission)
        self.assertNotEqual(result.job.session_id, current.session_id)
        self.assertEqual(result.job.session_generation, current.generation + 1)


class ConfigTests(unittest.TestCase):
    def test_marker_part_constant_is_stable(self) -> None:
        self.assertEqual(HANDOFF_PART_MARKER, "[[hub-handoff:v1]]")


if __name__ == "__main__":
    unittest.main()
