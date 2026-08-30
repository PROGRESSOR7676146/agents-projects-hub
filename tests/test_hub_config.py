from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.hub_config import (
    HubConfigError,
    load_codex_worker_config,
    load_controller_config,
    load_external_worker_config,
    load_hub_config,
)


class HubConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.token = self.base / "codex-token"
        self.token.write_text("123456:secret-token-value", encoding="utf-8")
        self.token.chmod(0o600)
        self.registry = self.base / "projects.json"
        self.registry.write_text(
            json.dumps({"schema_version": 1, "allowed_roots": [], "projects": []}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_config(self, **overrides: object) -> Path:
        document = {
            "schema_version": 1,
            "owner_user_ids": [123456789],
            "registry_path": str(self.registry),
            "state_path": str(self.base / "state.db"),
            "projects": [{"project_id": "alpha", "telegram_chat_id": -1001234567890}],
            "agents": [
                {
                    "agent_id": "codex",
                    "display_name": "Codex",
                    "telegram_username": "project_codex_bot",
                    "runtime": "codex",
                    "token_file": str(self.token),
                    "terminal_enabled": True,
                },
                {
                    "agent_id": "hermes",
                    "display_name": "Hermes",
                    "telegram_username": "project_hermes_bot",
                    "runtime": "hermes",
                    "managed_externally": True,
                    "terminal_enabled": False,
                },
            ],
        }
        document.update(overrides)
        path = self.base / "hub.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_loads_token_by_file_reference_without_embedding_secret(self) -> None:
        path = self.write_config()
        config = load_hub_config(path)
        self.assertEqual(config.require_agent("codex").token_file, self.token.resolve())
        self.assertEqual(
            config.codex_socket_path,
            Path.home() / ".codex/app-server-control/app-server-control.sock",
        )
        self.assertFalse(config.manage_codex_server)
        self.assertEqual(config.terminal.backend, "auto")
        self.assertNotIn("secret-token-value", path.read_text(encoding="utf-8"))

    def test_worker_loader_does_not_open_or_depend_on_telegram_token_file(self) -> None:
        path = self.write_config(dispatch_mode="queue", queue_runtime="external")
        self.token.unlink()
        with self.assertRaisesRegex(HubConfigError, "token_file"):
            load_hub_config(path)
        config = load_codex_worker_config(path)
        self.assertEqual(config.require_agent("codex").token_file, self.token.resolve())

    def test_controller_loader_validates_only_configured_hub_ingress_token(self) -> None:
        hub_token = self.base / "hub-token"
        hub_token.write_text("654321:hub-token-value", encoding="utf-8")
        hub_token.chmod(0o600)
        path = self.write_config(
            hub_bot={
                "telegram_username": "project_hub_bot",
                "token_file": str(hub_token),
            },
            dispatch_mode="queue",
            queue_runtime="external",
            outbox_runtime="external",
        )
        self.token.unlink()

        config = load_controller_config(path)

        self.assertEqual(config.hub_bot.token_file, hub_token.resolve())  # type: ignore[union-attr]
        self.assertEqual(config.require_agent("codex").token_file, self.token.resolve())

    def test_controller_loader_uses_codex_token_for_legacy_ingress_only(self) -> None:
        unrelated = self.base / "opencode-token"
        agents = [
            {
                "agent_id": "codex",
                "display_name": "Codex",
                "telegram_username": "project_codex_bot",
                "runtime": "codex",
                "token_file": str(self.token),
                "terminal_enabled": True,
            },
            {
                "agent_id": "opencode",
                "display_name": "OpenCode",
                "telegram_username": "project_opencode_bot",
                "runtime": "opencode",
                "token_file": str(unrelated),
                "terminal_enabled": False,
            },
        ]

        config = load_controller_config(self.write_config(agents=agents))

        self.assertIsNone(config.hub_bot)
        self.assertEqual(config.require_agent("codex").token_file, self.token.resolve())

    def test_external_worker_agent_ids_default_to_codex_and_validate_local_runtimes(self) -> None:
        config = load_hub_config(self.write_config(dispatch_mode="queue", queue_runtime="external"))
        self.assertEqual(config.external_worker_agent_ids, ("codex",))

        agents = [
            {
                "agent_id": "codex",
                "display_name": "Codex",
                "telegram_username": "project_codex_bot",
                "runtime": "codex",
                "token_file": str(self.token),
                "terminal_enabled": True,
            },
            {
                "agent_id": "opencode",
                "display_name": "OpenCode",
                "telegram_username": "project_opencode_bot",
                "runtime": "opencode",
                "token_file": str(self.base / "opencode-token"),
                "terminal_enabled": False,
            },
        ]
        config = load_external_worker_config(
            self.write_config(
                dispatch_mode="queue",
                queue_runtime="external",
                external_worker_agent_ids=["codex", "opencode"],
                agents=agents,
            )
        )
        self.assertEqual(config.external_worker_agent_ids, ("codex", "opencode"))
        with self.assertRaisesRegex(HubConfigError, "local runtime"):
            load_hub_config(
                self.write_config(
                    dispatch_mode="queue",
                    queue_runtime="external",
                    external_worker_agent_ids=["hermes"],
                )
            )

    def test_rejects_shared_locally_managed_agent_token_file(self) -> None:
        duplicate = {
            "agent_id": "opencode",
            "display_name": "OpenCode",
            "telegram_username": "project_opencode_bot",
            "runtime": "opencode",
            "token_file": str(self.token),
            "terminal_enabled": False,
        }
        with self.assertRaisesRegex(HubConfigError, "distinct token_file"):
            load_hub_config(
                self.write_config(
                    agents=[
                        {
                            "agent_id": "codex",
                            "display_name": "Codex",
                            "telegram_username": "project_codex_bot",
                            "runtime": "codex",
                            "token_file": str(self.token),
                            "terminal_enabled": True,
                        },
                        duplicate,
                    ]
                )
            )

    def test_loads_optional_hub_bot_as_a_separate_identity(self) -> None:
        hub_token = self.base / "hub-token"
        hub_token.write_text("654321:hub-token-value", encoding="utf-8")
        hub_token.chmod(0o600)

        config = load_hub_config(
            self.write_config(
                hub_bot={
                    "telegram_username": "project_hub_bot",
                    "token_file": str(hub_token),
                },
                dispatch_mode="queue",
                queue_runtime="external",
                outbox_runtime="external",
            )
        )

        self.assertIsNotNone(config.hub_bot)
        assert config.hub_bot is not None
        self.assertEqual(config.hub_bot.telegram_username, "project_hub_bot")
        self.assertEqual(config.hub_bot.token_file, hub_token.resolve())
        self.assertNotIsInstance(config.hub_bot, type(config.require_agent("codex")))

    def test_hub_bot_rejects_coupled_inline_or_embedded_runtime(self) -> None:
        hub_token = self.base / "hub-token"
        hub_token.write_text("654321:hub-token-value", encoding="utf-8")
        hub_token.chmod(0o600)
        with self.assertRaisesRegex(HubConfigError, "external workers and external outbox"):
            load_hub_config(
                self.write_config(
                    hub_bot={
                        "telegram_username": "project_hub_bot",
                        "token_file": str(hub_token),
                    }
                )
            )

    def test_hub_bot_rejects_local_runtime_without_isolated_worker_support(self) -> None:
        hub_token = self.base / "hub-token"
        hub_token.write_text("654321:hub-token-value", encoding="utf-8")
        hub_token.chmod(0o600)
        agents = [
            {
                "agent_id": "codex",
                "display_name": "Codex",
                "telegram_username": "project_codex_bot",
                "runtime": "codex",
                "token_file": str(self.token),
                "terminal_enabled": True,
            },
            {
                "agent_id": "gemini",
                "display_name": "Gemini",
                "telegram_username": "project_gemini_bot",
                "runtime": "gemini",
                "token_file": str(self.base / "unused-gemini-token"),
                "terminal_enabled": False,
            },
        ]
        with self.assertRaisesRegex(HubConfigError, "unisolated local runtime.*gemini"):
            load_controller_config(
                self.write_config(
                    agents=agents,
                    hub_bot={
                        "telegram_username": "project_hub_bot",
                        "token_file": str(hub_token),
                    },
                    dispatch_mode="queue",
                    queue_runtime="external",
                    outbox_runtime="external",
                    external_worker_agent_ids=["codex"],
                )
            )

    def test_hub_bot_remains_optional_for_backward_compatible_configs(self) -> None:
        self.assertIsNone(load_hub_config(self.write_config()).hub_bot)

    def test_dispatch_mode_defaults_to_inline_and_accepts_queue(self) -> None:
        self.assertEqual(load_hub_config(self.write_config()).dispatch_mode, "inline")
        self.assertEqual(load_hub_config(self.write_config()).queue_runtime, "embedded")
        self.assertEqual(load_hub_config(self.write_config()).outbox_runtime, "controller")
        self.assertEqual(
            load_hub_config(self.write_config(dispatch_mode="queue")).dispatch_mode,
            "queue",
        )
        self.assertEqual(
            load_hub_config(
                self.write_config(dispatch_mode="queue", queue_runtime="external")
            ).queue_runtime,
            "external",
        )
        with self.assertRaisesRegex(HubConfigError, "dispatch_mode"):
            load_hub_config(self.write_config(dispatch_mode="background"))
        with self.assertRaisesRegex(HubConfigError, "queue_runtime"):
            load_hub_config(self.write_config(queue_runtime="remote"))
        with self.assertRaisesRegex(HubConfigError, "queue_runtime external requires"):
            load_hub_config(self.write_config(queue_runtime="external"))
        self.assertEqual(
            load_hub_config(
                self.write_config(
                    dispatch_mode="queue",
                    queue_runtime="external",
                    outbox_runtime="external",
                )
            ).outbox_runtime,
            "external",
        )
        with self.assertRaisesRegex(HubConfigError, "outbox_runtime"):
            load_hub_config(self.write_config(outbox_runtime="remote"))
        with self.assertRaisesRegex(HubConfigError, "external queue runtime"):
            load_hub_config(self.write_config(outbox_runtime="external"))

    def test_rejects_inline_hub_bot_token(self) -> None:
        with self.assertRaisesRegex(HubConfigError, "inline token"):
            load_hub_config(
                self.write_config(
                    hub_bot={
                        "telegram_username": "project_hub_bot",
                        "token": "must-not-be-here",
                        "token_file": str(self.token),
                    }
                )
            )

    def test_rejects_hub_bot_username_that_collides_with_a_provider(self) -> None:
        with self.assertRaisesRegex(HubConfigError, "duplicates an agent"):
            load_hub_config(
                self.write_config(
                    hub_bot={
                        "telegram_username": "project_codex_bot",
                        "token_file": str(self.token),
                    }
                )
            )

    def test_rejects_hub_bot_token_file_shared_with_a_provider(self) -> None:
        with self.assertRaisesRegex(HubConfigError, "duplicates an agent token_file"):
            load_hub_config(
                self.write_config(
                    hub_bot={
                        "telegram_username": "project_hub_bot",
                        "token_file": str(self.token),
                    }
                )
            )

    def test_rejects_world_readable_hub_bot_token_file(self) -> None:
        hub_token = self.base / "hub-token"
        hub_token.write_text("654321:hub-token-value", encoding="utf-8")
        hub_token.chmod(0o644)
        with self.assertRaisesRegex(HubConfigError, "token_file for hub_bot must have mode 0600"):
            load_hub_config(
                self.write_config(
                    hub_bot={
                        "telegram_username": "project_hub_bot",
                        "token_file": str(hub_token),
                    }
                )
            )

    def test_direct_messages_require_an_explicit_registered_project(self) -> None:
        config = load_hub_config(self.write_config(direct_message_project_id="alpha"))
        self.assertEqual(config.direct_message_project_id, "alpha")
        with self.assertRaisesRegex(HubConfigError, "direct_message_project_id"):
            load_hub_config(self.write_config(direct_message_project_id="missing"))

    def test_loads_single_operational_alert_topic_from_registered_hub_project(self) -> None:
        config = load_hub_config(
            self.write_config(
                projects=[
                    {"project_id": "hub", "telegram_chat_id": -1000000000001},
                    {"project_id": "alpha", "telegram_chat_id": -1001234567890},
                ],
                operational_alerts={"project_id": "hub", "telegram_thread_id": 41},
            )
        )
        self.assertEqual(config.operational_alerts.telegram_chat_id, -1000000000001)
        self.assertEqual(config.operational_alerts.telegram_thread_id, 41)

    def test_rejects_operational_alert_destination_outside_registered_projects(self) -> None:
        with self.assertRaisesRegex(HubConfigError, "operational_alerts.project_id"):
            load_hub_config(
                self.write_config(
                    operational_alerts={"project_id": "missing", "telegram_thread_id": 41}
                )
            )

    def test_rejects_project_chat_as_operational_alert_destination(self) -> None:
        with self.assertRaisesRegex(HubConfigError, "must be hub"):
            load_hub_config(
                self.write_config(
                    operational_alerts={"project_id": "alpha", "telegram_thread_id": 41}
                )
            )

    def test_rejects_non_boolean_manage_codex_server(self) -> None:
        with self.assertRaisesRegex(HubConfigError, "manage_codex_server"):
            load_hub_config(self.write_config(manage_codex_server="no"))

    def test_loads_three_character_codex_account_hints(self) -> None:
        config = load_hub_config(self.write_config(codex_account_hints={"1": "acc", "2": "alt"}))
        self.assertEqual(config.codex_account_hints, {1: "acc", 2: "alt"})

    def test_rejects_group_chat_id_that_is_not_supergroup_shaped(self) -> None:
        with self.assertRaisesRegex(HubConfigError, "telegram_chat_id"):
            load_hub_config(
                self.write_config(projects=[{"project_id": "alpha", "telegram_chat_id": 123}])
            )

    def test_allows_unbound_group_only_during_bootstrap(self) -> None:
        path = self.write_config(projects=[{"project_id": "alpha", "telegram_chat_id": None}])
        with self.assertRaisesRegex(HubConfigError, "unbound"):
            load_hub_config(path)
        config = load_hub_config(path, allow_unbound=True)
        self.assertIsNone(config.projects[0].telegram_chat_id)

    def test_rejects_world_readable_token_file(self) -> None:
        self.token.chmod(0o644)
        with self.assertRaisesRegex(HubConfigError, "0600"):
            load_hub_config(self.write_config())

    def test_rejects_inline_token_field(self) -> None:
        agents = [
            {
                "agent_id": "codex",
                "display_name": "Codex",
                "telegram_username": "project_codex_bot",
                "runtime": "codex",
                "token": "must-not-be-here",
                "token_file": str(self.token),
            }
        ]
        with self.assertRaisesRegex(HubConfigError, "inline token"):
            load_hub_config(self.write_config(agents=agents))

    def test_rejects_telegram_username_that_cannot_be_a_bot(self) -> None:
        agents = [
            {
                "agent_id": "opencode",
                "display_name": "OpenCode",
                "telegram_username": "opencode",
                "runtime": "opencode",
                "managed_externally": True,
            }
        ]
        with self.assertRaisesRegex(HubConfigError, "must end in bot"):
            load_hub_config(self.write_config(agents=agents))

    def test_loads_safe_agent_service_unit(self) -> None:
        agents = [
            {
                "agent_id": "opencode",
                "display_name": "OpenCode",
                "telegram_username": "project_opencode_bot",
                "runtime": "opencode",
                "managed_externally": True,
                "service_unit": "agents-projects-hub@opencode.service",
            }
        ]
        config = load_hub_config(self.write_config(agents=agents))
        self.assertEqual(
            config.require_agent("opencode").service_unit,
            "agents-projects-hub@opencode.service",
        )

    def test_rejects_unsafe_agent_service_unit(self) -> None:
        agents = [
            {
                "agent_id": "opencode",
                "display_name": "OpenCode",
                "telegram_username": "project_opencode_bot",
                "runtime": "opencode",
                "managed_externally": True,
                "service_unit": "../../opencode.service",
            }
        ]
        with self.assertRaisesRegex(HubConfigError, "service_unit"):
            load_hub_config(self.write_config(agents=agents))

    def test_state_parent_is_not_required_to_exist_during_config_parse(self) -> None:
        missing = self.base / "private" / "state.db"
        config = load_hub_config(self.write_config(state_path=str(missing)))
        self.assertEqual(config.state_path, missing.resolve())

    def test_loads_explicit_terminal_backend(self) -> None:
        config = load_hub_config(
            self.write_config(
                terminal={"backend": "linux", "program": "kitty", "wsl_distro": "Ubuntu"}
            )
        )
        self.assertEqual(config.terminal.backend, "linux")
        self.assertEqual(config.terminal.program, "kitty")

    def test_loads_recovery_plane_without_embedding_credentials(self) -> None:
        hermes_config = self.base / "hermes.yaml"
        tlive_config = self.base / "tlive.json"
        hermes_config.write_text("model: provider-selected\n", encoding="utf-8")
        tlive_config.write_text("{}\n", encoding="utf-8")
        hermes_config.chmod(0o600)
        tlive_config.chmod(0o600)
        config = load_hub_config(
            self.write_config(
                recovery_plane={
                    "enabled": True,
                    "hermes_service": "hermes-gateway.service",
                    "tlive_service": "tlive.service",
                    "hermes_config_path": str(hermes_config),
                    "tlive_config_path": str(tlive_config),
                }
            )
        )
        self.assertTrue(config.recovery_plane.enabled)
        self.assertEqual(config.recovery_plane.hermes_service, "hermes-gateway.service")
        self.assertEqual(config.recovery_plane.tlive_config_path, tlive_config.resolve())
        self.assertEqual(config.recovery_plane.hermes_notify_target, "telegram")

    def test_rejects_unsafe_recovery_service_unit(self) -> None:
        with self.assertRaisesRegex(HubConfigError, "hermes_service"):
            load_hub_config(
                self.write_config(
                    recovery_plane={
                        "enabled": True,
                        "hermes_service": "../../escape.service",
                    }
                )
            )

    def test_loads_private_runtime_home_for_isolated_provider_account(self) -> None:
        runtime_home = self.base / "gemini-account-a"
        runtime_home.mkdir(mode=0o700)
        agent = {
            "agent_id": "gemini-a",
            "display_name": "Gemini A",
            "telegram_username": "project_gemini_a_bot",
            "runtime": "gemini",
            "managed_externally": True,
            "runtime_home": str(runtime_home),
        }
        config = load_hub_config(self.write_config(agents=[agent]))
        self.assertEqual(config.require_agent("gemini-a").runtime_home, runtime_home.resolve())

    def test_rejects_world_readable_runtime_home(self) -> None:
        runtime_home = self.base / "gemini-account-a"
        runtime_home.mkdir(mode=0o755)
        runtime_home.chmod(0o755)
        agent = {
            "agent_id": "gemini-a",
            "display_name": "Gemini A",
            "telegram_username": "project_gemini_a_bot",
            "runtime": "gemini",
            "managed_externally": True,
            "runtime_home": str(runtime_home),
        }
        with self.assertRaisesRegex(HubConfigError, "runtime_home.*0700"):
            load_hub_config(self.write_config(agents=[agent]))


if __name__ == "__main__":
    unittest.main()
