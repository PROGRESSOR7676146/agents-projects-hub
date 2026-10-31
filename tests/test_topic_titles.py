from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.telegram import parse_topic_message, parse_topic_rename
from tests.hub_service_harness import CHAT_ID, OWNER_ID, THREAD_ID, HubHarness, text_update

STRANGER_ID = 424242


def rename_update(
    name: object,
    *,
    thread_id: object = THREAD_ID,
    general: bool = False,
    sender_id: int = OWNER_ID,
    chat_id: int = CHAT_ID,
    is_forum: bool = True,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": 900,
        "from": {"id": sender_id, "is_bot": False},
        "chat": {"id": chat_id, "type": "supergroup", "title": "Example", "is_forum": is_forum},
        "forum_topic_edited": {"name": name},
    }
    if not general:
        message["message_thread_id"] = thread_id
        message["is_topic_message"] = True
    return {"update_id": 900, "message": message}


class TopicRenameParsingTests(unittest.TestCase):
    def test_topic_and_general_renames_are_parsed(self) -> None:
        topic = parse_topic_rename(rename_update("  Release   plan "))
        general = parse_topic_rename(rename_update("Main", general=True))
        assert topic is not None and general is not None
        self.assertEqual((topic.thread_id, topic.title), (THREAD_ID, "Release plan"))
        self.assertEqual((general.thread_id, general.title), (1, "Main"))
        long_title = parse_topic_rename(rename_update("x" * 300))
        assert long_title is not None
        self.assertEqual(len(long_title.title), 128)

    def test_malformed_thread_ids_never_mean_general(self) -> None:
        for value in ("42", 0, -3, True, 1.0, None):
            with self.subTest(value=value):
                self.assertIsNone(parse_topic_rename(rename_update("Main", thread_id=value)))

    def test_icon_only_edits_and_non_forums_are_ignored(self) -> None:
        self.assertIsNone(parse_topic_rename(rename_update(None)))
        self.assertIsNone(parse_topic_rename(rename_update("   ")))
        self.assertIsNone(parse_topic_rename(rename_update("Main", is_forum=False)))
        self.assertIsNone(parse_topic_rename(text_update(1, "plain text")))

    def test_topic_message_carries_its_creation_name(self) -> None:
        update = text_update(5, "hello")
        message = update["message"]
        assert isinstance(message, dict)
        message["reply_to_message"] = {
            "message_id": THREAD_ID,
            "forum_topic_created": {"name": "Design review"},
        }
        parsed = parse_topic_message(update)
        assert parsed is not None
        self.assertEqual(parsed.topic_name, "Design review")
        self.assertIsNone(parsed.reply_to_message_id)
        message["reply_to_message"] = {"message_id": 3, "forum_topic_created": {"name": "Other"}}
        not_anchor = parse_topic_message(update)
        assert not_anchor is not None
        self.assertIsNone(not_anchor.topic_name)


class TopicRenameServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.hub = HubHarness(Path(directory.name))
        self.addCleanup(self.hub.close)

    def title(self) -> str:
        return self.hub.topic().title

    def counts(self) -> tuple[int, int]:
        connection = self.hub.service.state._connection
        jobs = connection.execute("SELECT count(*) FROM provider_jobs").fetchone()[0]
        topics = connection.execute("SELECT count(*) FROM topics").fetchone()[0]
        return jobs, topics

    def test_owner_rename_updates_the_stored_title_without_changing_identity(self) -> None:
        before = self.hub.topic()
        sent, counts = len(self.hub.telegram.sent), self.counts()
        self.assertTrue(self.hub.service.handle_update(rename_update("Release plan")))
        after = self.hub.topic()
        self.assertEqual(after.title, "Release plan")
        self.assertEqual(
            (after.topic_id, after.execution_scope), (before.topic_id, before.execution_scope)
        )
        # A rename is display metadata: no reply, no provider job, no other topic.
        self.assertEqual((len(self.hub.telegram.sent), self.counts()), (sent, counts))

    def test_general_topic_rename_reaches_the_stored_general_topic(self) -> None:
        update = text_update(11, "/menu")
        message = update["message"]
        assert isinstance(message, dict)
        del message["message_thread_id"]
        del message["is_topic_message"]
        message["chat"] = {
            "id": CHAT_ID,
            "type": "supergroup",
            "title": "Example",
            "is_forum": True,
        }
        self.hub.service.handle_update(update)
        self.assertTrue(self.hub.service.handle_update(rename_update("Main", general=True)))
        general = self.hub.service.state.find_topic(CHAT_ID, 1)
        assert general is not None
        self.assertEqual(general.title, "Main")

    def test_malformed_or_mixed_edit_messages_are_consumed_without_dispatch(self) -> None:
        original = self.title()
        sent, counts = len(self.hub.telegram.sent), self.counts()
        mixed = rename_update(None)
        message = mixed["message"]
        assert isinstance(message, dict)
        message["text"] = "please run the deployment"
        for update in (
            mixed,
            rename_update("Main", thread_id="42"),
            rename_update("Main", thread_id=0),
        ):
            self.assertFalse(self.hub.service.handle_update(update))
        self.assertEqual(self.title(), original)
        self.assertEqual((len(self.hub.telegram.sent), self.counts()), (sent, counts))

    def test_unauthorized_or_unbound_renames_change_nothing(self) -> None:
        original = self.title()
        counts = self.counts()
        self.assertFalse(
            self.hub.service.handle_update(rename_update("Hijacked", sender_id=STRANGER_ID))
        )
        self.assertFalse(
            self.hub.service.handle_update(rename_update("Elsewhere", chat_id=-1001111111111))
        )
        self.assertEqual(self.title(), original)
        self.assertEqual(self.counts(), counts)

    def test_creation_name_replaces_only_a_placeholder(self) -> None:
        update = text_update(7, "/menu")
        message = update["message"]
        assert isinstance(message, dict)
        message["reply_to_message"] = {
            "message_id": THREAD_ID,
            "forum_topic_created": {"name": "Design review"},
        }
        self.hub.service.handle_update(update)
        self.assertEqual(self.title(), "Design review")
        self.hub.service.handle_update(rename_update("Renamed later"))
        message["message_id"] = 8
        update["update_id"] = 8
        self.hub.service.handle_update(update)
        self.assertEqual(self.title(), "Renamed later")


if __name__ == "__main__":
    unittest.main()
