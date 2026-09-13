from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from test_codex_appserver import FakeTransport

from hermes_codex_router.codex_appserver import CodexAppServerClient


class CodexSessionDiscoveryTests(unittest.TestCase):
    def test_list_threads_uses_exact_root_and_drops_preview_and_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            responses = [
                {"id": 1, "result": {}},
                {
                    "id": 2,
                    "result": {
                        "data": [
                            {
                                "id": "example-thread",
                                "cwd": str(root),
                                "name": None,
                                "preview": "private first prompt",
                                "path": "/private/store/path",
                                "ephemeral": False,
                                "modelProvider": "openai",
                                "source": "cli",
                                "status": {"type": "notLoaded"},
                                "createdAt": 1,
                                "updatedAt": 2,
                                "turns": [],
                            }
                        ],
                        "nextCursor": None,
                    },
                },
            ]
            transport = FakeTransport(responses)
            client = CodexAppServerClient(transport)
            client.initialize()
            sessions = client.list_connectable_threads(root=root)
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].thread_id, "example-thread")
            serialized = json.dumps(sessions[0].to_public_dict())
            self.assertNotIn("private first prompt", serialized)
            self.assertNotIn("/private/store/path", serialized)


if __name__ == "__main__":
    unittest.main()
