from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path

import test_codex_worker as worker_fixtures

from hermes_codex_router.cli import main
from hermes_codex_router.codex_appserver import ConnectableCodexThread
from hermes_codex_router.hub_config import HubTelegramBot
from hermes_codex_router.session_connect_cli import prepare_connect_code
from hermes_codex_router.state import HubState


class SessionConnectCliTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.root = fixture.registry.projects[0].root
        self.config = replace(
            fixture.config,
            hub_bot=HubTelegramBot("example_hub_bot", Path("/tmp/example-token")),
            outbox_runtime="external",
            external_worker_agent_ids=("codex",),
        )
        self.config.registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_roots": [str(self.root.parent)],
                    "projects": [
                        {
                            "project_id": "example-project",
                            "display_name": "Example",
                            "topic_name": "Example",
                            "root": str(self.root),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        state = HubState.open(self.config.state_path)
        state.close()

    def test_missing_config_does_not_guess_a_private_path(self) -> None:
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["session", "connect", "--json"]), 2)
        self.assertEqual(json.loads(output.getvalue())["reason_code"], "configuration_required")

    def test_noninteractive_helper_issues_only_a_hashed_short_lived_code(self) -> None:
        result = prepare_connect_code(
            self.config,
            owner_user_id=42,
            project_id="example-project",
            codex_thread_id="example-cli-thread",
            interactive=False,
            lister=lambda _config, root: (
                ConnectableCodexThread("example-cli-thread", "Сессия · 2026-09-12 · thread", 10),
            ),
        )
        command = str(result["telegram_command"])
        code = command.split()[1]
        self.assertEqual(len(code), 10)
        state = HubState.open(self.config.state_path)
        self.addCleanup(state.close)
        row = state._connection.execute(
            "SELECT code_digest,consumed_at FROM session_connect_codes"
        ).fetchone()
        self.assertEqual(len(row["code_digest"]), 64)
        self.assertNotEqual(row["code_digest"], code)
        self.assertIsNone(row["consumed_at"])
        self.assertEqual(
            state._connection.execute("SELECT COUNT(*) FROM provider_jobs").fetchone()[0], 0
        )


if __name__ == "__main__":
    unittest.main()
