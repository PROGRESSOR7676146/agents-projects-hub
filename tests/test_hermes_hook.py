from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


class HermesHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_exports_only_visible_owner_telegram_turn_fields(self) -> None:
        handler_path = (
            Path(__file__).resolve().parents[1] / "integrations/hermes-project-hub-hook/handler.py"
        )
        spec = importlib.util.spec_from_file_location("test_hermes_hook_handler", handler_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(
            os.environ,
            {
                "HERMES_PROJECT_HUB_OWNER_IDS": "42",
                "HERMES_PROJECT_HUB_STATE": "/tmp/test-hermes-hook.db",
            },
        ):
            spec.loader.exec_module(module)

        recorded: list[dict[str, Any]] = []
        setattr(module, "record_external_turn", lambda *_args, **kwargs: recorded.append(kwargs))
        await module.handle(
            "agent:end",
            {
                "platform": "telegram",
                "user_id": 42,
                "chat_id": -1001,
                "thread_id": 73,
                "session_id": "session-1",
                "model": "model-1",
                "provider": "provider-1",
                "message": "visible question",
                "response": "visible answer",
                "reasoning": "must not be exported",
            },
        )

        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["user_excerpt"], "visible question")
        self.assertEqual(recorded[0]["response_excerpt"], "visible answer")
        self.assertNotIn("reasoning", recorded[0])

    async def test_ignores_non_owner_and_non_telegram_events(self) -> None:
        handler_path = (
            Path(__file__).resolve().parents[1] / "integrations/hermes-project-hub-hook/handler.py"
        )
        spec = importlib.util.spec_from_file_location("test_hermes_hook_reject", handler_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"HERMES_PROJECT_HUB_OWNER_IDS": "42"}):
            spec.loader.exec_module(module)
        recorded: list[dict[str, Any]] = []
        setattr(module, "record_external_turn", lambda *_args, **kwargs: recorded.append(kwargs))

        await module.handle("agent:end", {"platform": "telegram", "user_id": 7})
        await module.handle("agent:end", {"platform": "cli", "user_id": 42})
        await module.handle("agent:start", {"platform": "telegram", "user_id": 42})
        self.assertEqual(recorded, [])


if __name__ == "__main__":
    unittest.main()
