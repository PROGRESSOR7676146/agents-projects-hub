from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from hermes_codex_router import hermes_plugin
from hermes_codex_router.external_admission import is_hub_chat

HUB_CHAT = -1001234567890
OTHER_CHAT = -1002222222222


class _Filter:
    def __init__(self, name: str) -> None:
        self.name = name

    def __and__(self, other: _Filter) -> _Filter:
        return _Filter(f"({self.name} & {other.name})")

    def __invert__(self) -> _Filter:
        return _Filter(f"~{self.name}")


class _Stop(Exception):
    pass


class _MessageHandler:
    def __init__(self, handler_filter: _Filter, callback: Any) -> None:
        self.filter = handler_filter
        self.callback = callback


class _ScopeChat:
    def __init__(self, chat_id: int) -> None:
        self.chat_id = chat_id


def _fake_telegram() -> dict[str, types.ModuleType]:
    telegram = types.ModuleType("telegram")
    setattr(telegram, "BotCommandScopeChat", _ScopeChat)
    ext = types.ModuleType("telegram.ext")
    filters = types.SimpleNamespace(
        TEXT=_Filter("TEXT"),
        COMMAND=_Filter("COMMAND"),
        ChatType=types.SimpleNamespace(GROUPS=_Filter("GROUPS")),
    )
    setattr(ext, "ApplicationHandlerStop", _Stop)
    setattr(ext, "MessageHandler", _MessageHandler)
    setattr(ext, "filters", filters)
    return {"telegram": telegram, "telegram.ext": ext}


class _Bot:
    def __init__(self) -> None:
        self.deleted: list[int] = []
        self.fail = False

    async def set_my_commands(self, commands: list[Any], *, scope: _ScopeChat) -> bool:
        if self.fail:
            raise OSError("network")
        # An explicitly empty chat scope, never a deletion that would inherit
        # Hermes' group-wide menu.
        assert commands == []
        self.deleted.append(scope.chat_id)
        return True

    async def delete_my_commands(self, *, scope: _ScopeChat) -> bool:
        raise AssertionError("deleting the scope falls back to broader menus")


class _Adapter:
    def __init__(self) -> None:
        self._bot = _Bot()
        self.registered: list[int] = []
        self.addressed = False

    async def _ensure_forum_commands(self, message: Any) -> None:
        self.registered.append(message.chat.id)

    def _effective_update_message(self, update: Any) -> Any:
        return update.message

    def _message_mentions_bot(self, message: Any) -> bool:
        return self.addressed

    def _extract_bot_mention_usernames(self, message: Any, username: str) -> list[str]:
        return []

    def _current_bot_username(self) -> str:
        return "hermes_bot"


def _message(chat_id: int, thread_id: int = 7) -> Any:
    return types.SimpleNamespace(
        chat=types.SimpleNamespace(id=chat_id, type="supergroup"),
        message_thread_id=thread_id,
        text="/new",
    )


class HermesPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.state = Path(directory.name) / "state.db"
        connection = sqlite3.connect(self.state)
        connection.execute(
            "CREATE TABLE topics (chat_id INTEGER, thread_id INTEGER, active_agent_id TEXT)"
        )
        connection.execute("INSERT INTO topics VALUES (?, 7, 'codex')", (HUB_CHAT,))
        connection.commit()
        connection.close()
        patcher = mock.patch.dict("os.environ", {"HERMES_PROJECT_HUB_STATE": str(self.state)})
        patcher.start()
        self.addCleanup(patcher.stop)
        modules = mock.patch.dict(sys.modules, _fake_telegram())
        modules.start()
        self.addCleanup(modules.stop)
        self.adapter = _Adapter()
        self.handlers: list[tuple[_MessageHandler, int]] = []
        application = types.SimpleNamespace(
            add_handler=lambda handler, group: self.handlers.append((handler, group))
        )
        wires: list[Any] = []
        hermes_plugin.register(types.SimpleNamespace(register_telegram_handler=wires.append))
        wires[0](application, self.adapter)

    def handler(self, name: str) -> Any:
        for handler, group in self.handlers:
            if name in handler.filter.name and f"~{name}" not in handler.filter.name:
                self.assertEqual(group, -20)
                return handler.callback
        raise AssertionError(name)

    def command(self, chat_id: int) -> None:
        update = types.SimpleNamespace(message=_message(chat_id))
        asyncio.run(self.handler("COMMAND")(update, None))

    def test_bare_commands_in_a_hub_group_stay_with_the_hub(self) -> None:
        with self.assertRaises(_Stop):
            self.command(HUB_CHAT)
        with self.assertRaises(_Stop):
            self.command(HUB_CHAT)
        self.assertEqual(self.adapter._bot.deleted, [HUB_CHAT])

    def test_commands_addressed_to_hermes_pass(self) -> None:
        self.adapter.addressed = True
        self.command(HUB_CHAT)
        self.assertEqual(self.adapter._bot.deleted, [HUB_CHAT])

    def test_other_groups_keep_native_hermes_commands(self) -> None:
        self.command(OTHER_CHAT)
        self.assertEqual(self.adapter._bot.deleted, [])

    def test_ordinary_text_in_a_hub_group_also_clears_the_menu(self) -> None:
        update = types.SimpleNamespace(message=_message(HUB_CHAT))
        with self.assertRaises(_Stop):
            asyncio.run(self.handler("TEXT")(update, None))
        self.assertEqual(self.adapter._bot.deleted, [HUB_CHAT])

    def test_hermes_never_registers_its_menu_in_a_hub_group(self) -> None:
        asyncio.run(self.adapter._ensure_forum_commands(_message(HUB_CHAT)))
        asyncio.run(self.adapter._ensure_forum_commands(_message(OTHER_CHAT)))
        self.assertEqual(self.adapter.registered, [OTHER_CHAT])
        self.assertEqual(self.adapter._bot.deleted, [HUB_CHAT])

    def test_an_inaccessible_state_still_stops_bare_commands(self) -> None:
        locked = self.state.parent / "locked"
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        with mock.patch.dict("os.environ", {"HERMES_PROJECT_HUB_STATE": str(locked / "s.db")}):
            with self.assertRaises(_Stop):
                self.command(OTHER_CHAT)

    def test_a_failed_menu_clear_is_retried(self) -> None:
        self.adapter._bot.fail = True
        with self.assertRaises(_Stop):
            self.command(HUB_CHAT)
        self.adapter._bot.fail = False
        with self.assertRaises(_Stop):
            self.command(HUB_CHAT)
        self.assertEqual(self.adapter._bot.deleted, [HUB_CHAT])


class HubChatLookupTests(unittest.TestCase):
    def test_lookup_is_fail_closed_only_for_an_unreadable_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            self.assertFalse(is_hub_chat(state, HUB_CHAT))
            state.write_bytes(b"not a database")
            self.assertTrue(is_hub_chat(state, HUB_CHAT))
            state.unlink()
            connection = sqlite3.connect(state)
            connection.execute("CREATE TABLE topics (chat_id INTEGER)")
            connection.execute("INSERT INTO topics VALUES (?)", (HUB_CHAT,))
            connection.commit()
            connection.close()
            self.assertTrue(is_hub_chat(state, HUB_CHAT))
            self.assertFalse(is_hub_chat(state, OTHER_CHAT))
            self.assertFalse(is_hub_chat(state, 12345))

    def test_an_inaccessible_state_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            locked = Path(directory) / "locked"
            locked.mkdir()
            locked.chmod(0)
            try:
                self.assertTrue(is_hub_chat(locked / "state.db", HUB_CHAT))
            finally:
                locked.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
