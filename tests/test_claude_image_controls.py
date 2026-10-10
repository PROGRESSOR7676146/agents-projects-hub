from __future__ import annotations

import json
import subprocess
import threading
import unittest
from unittest.mock import patch

from hermes_codex_router.claude_cli_capabilities import ClaudeCliCapabilityError
from hermes_codex_router.external_runtime import ExternalCliAdapter, ProviderUnavailableError
from hermes_codex_router.hub_config import HubConfigError, load_hub_config
from tests import test_claude_cli_capabilities as capabilities
from tests import test_claude_configured_catalog as config
from tests import test_claude_permissions_config as permissions
from tests.test_claude_image_input import SESSION, image


class ClaudeImageConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = config.ClaudeCatalogConfigurationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def load(self, **changes: object):
        self.fixture.load()
        path = self.fixture.fixture.base / "hub.json"
        document = json.loads(path.read_text())
        document.update(changes)
        path.write_text(json.dumps(document))
        return load_hub_config(path)

    def test_false_default_and_explicit_true_are_distinct(self) -> None:
        self.assertFalse(self.load().claude_image_input)
        self.assertFalse(self.load(claude_image_input=False).claude_image_input)
        self.assertTrue(self.load(claude_image_input=True).claude_image_input)

    def test_invalid_values_ownership_and_file_tool_combination_refuse(self) -> None:
        invalid = [dict(claude_image_input=value) for value in (None, 1, "true", {})]
        invalid.extend(
            (
                {"claude_image_input": True, "external_worker_agent_ids": ["codex"]},
                {
                    "claude_image_input": True,
                    "claude_file_permissions": permissions.ClaudePermissionsConfigTests().settings(),
                },
            )
        )
        with patch("subprocess.Popen", side_effect=AssertionError("no provider call")):
            for change in invalid:
                with (
                    self.subTest(change=change),
                    self.assertRaisesRegex(
                        HubConfigError,
                        "claude_image_input|claude runtime requires an external worker",
                    ),
                ):
                    self.load(**change)


class ClaudeImageCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = capabilities.ClaudeCliCapabilitiesTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def require_image(self):
        return self.fixture.capabilities.require(
            str(self.fixture.cli),
            cwd=self.fixture.root,
            environment=self.fixture.environment,
            interrupted=threading.Event(),
            image_input=True,
        )

    def test_text_success_cache_does_not_authorize_missing_image_advertisement(self) -> None:
        self.fixture.help_cli()
        self.fixture.require()
        with self.assertRaises(ClaudeCliCapabilityError):
            self.require_image()
        self.fixture.help_cli(
            capabilities.HELP
            + "  --input-format <format>  Values: text, stream-json\n  --replay-user-messages  Re-emit processed input\n"
        )
        self.assertEqual(self.require_image(), str(self.fixture.cli))

    def test_stream_json_must_be_advertised_in_its_own_stanza(self) -> None:
        self.fixture.help_cli(
            capabilities.HELP + "  --input-format <format>  text\nNotes: stream-json elsewhere\n"
        )
        with self.assertRaises(ClaudeCliCapabilityError):
            self.require_image()

    def test_injected_runner_and_other_providers_refuse_image_calls_before_spawn(self) -> None:
        for runtime in ("claude", "opencode", "antigravity"):
            with self.subTest(runtime=runtime):
                adapter = ExternalCliAdapter(
                    runtime,
                    run=lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
                )
                with (
                    patch.dict(
                        "os.environ",
                        {
                            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                            "ANTHROPIC_API_KEY": "example",
                        },
                        clear=True,
                    ),
                    patch("subprocess.Popen") as spawn,
                ):
                    with self.assertRaises(ProviderUnavailableError):
                        adapter.run_turn(
                            cwd=self.fixture.root,
                            prompt="Example caption",
                            new_session_id=SESSION if runtime == "claude" else None,
                            claude_images=(image(),),
                        )
                    spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
