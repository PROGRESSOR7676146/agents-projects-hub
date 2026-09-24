from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.catalog_refresh import native_codex_models, refresh_provider_catalogs
from hermes_codex_router.hub_config import AgentDefinition, HubConfig
from hermes_codex_router.provider_catalog import ProviderModel
from hermes_codex_router.provider_catalog_cache import ProviderCatalogCache


class CatalogRefreshTests(unittest.TestCase):
    def test_native_metadata_discovery_closes_connection_without_turns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self._config(Path(directory))
            with (
                patch("hermes_codex_router.catalog_refresh.UnixWebSocketTransport") as transport,
                patch("hermes_codex_router.catalog_refresh.CodexAppServerClient") as factory,
            ):
                client = factory.return_value
                client.list_models.return_value = (
                    {
                        "id": "gpt-6-astra",
                        "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                    },
                    {
                        "id": "claude-sonnet-4-6",
                        "modelProvider": "cliproxyapi",
                        "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                    },
                    {
                        "id": "gemini-3-flash",
                        "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
                    },
                )
                models = native_codex_models(replace(config, codex_model_provider="cliproxyapi"))
                transport.assert_called_once_with(config.codex_socket_path)
                self.assertEqual(
                    [call[0] for call in client.method_calls],
                    ["initialize", "list_models", "close"],
                )
                self.assertEqual(
                    models,
                    (
                        ProviderModel("gpt-6-astra", "gpt-6-astra", ("high",)),
                        ProviderModel("claude-sonnet-4-6", "claude-sonnet-4-6", ("high",)),
                    ),
                )

    def _config(self, root: Path) -> HubConfig:
        return HubConfig(
            schema_version=1,
            owner_user_ids=(1,),
            registry_path=root / "registry.json",
            state_path=root / "state.db",
            codex_socket_path=root / "codex.sock",
            manage_codex_server=False,
            terminal=None,  # type: ignore[arg-type]
            projects=(),
            agents=(
                AgentDefinition(
                    "codex", "Codex", "codex_bot", "codex", None, True, False, "gpt", "high"
                ),
            ),
            codex_multi_auth_executable=Path("/usr/bin/codex-multi-auth"),
        )

    def test_refreshes_stale_catalog_and_marks_new_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            cache = ProviderCatalogCache(root / "provider-model-catalogs.json")
            now = datetime(2026, 9, 4, tzinfo=timezone.utc)
            cache.store(
                "codex",
                (ProviderModel("old", "Old", ("high",)),),
                source_version="1",
                observed_at=now - timedelta(days=1),
            )
            payload = {
                "matrix": {
                    "entries": [
                        {"model": "old", "available": True, "supportedReasoningEfforts": ["high"]},
                        {
                            "model": "new",
                            "available": True,
                            "supportedReasoningEfforts": ["low", "high"],
                        },
                    ]
                }
            }

            def run(argv, **kwargs):
                if argv[1] == "--version":
                    return subprocess.CompletedProcess(argv, 0, "1.2.3\n", "")
                return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

            result = refresh_provider_catalogs(config, now=now, run=run)
            self.assertEqual(result.refreshed, ("codex",))
            self.assertEqual(result.added, {"codex": ("new",)})
            self.assertFalse(cache.is_stale("codex", now=now))

    def test_failure_preserves_last_known_good_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = self._config(root)
            cache = ProviderCatalogCache(root / "provider-model-catalogs.json")
            now = datetime(2026, 9, 4, tzinfo=timezone.utc)
            cache.store(
                "codex",
                (ProviderModel("old", "Old", ("high",)),),
                source_version="1",
                observed_at=now - timedelta(days=1),
            )
            result = refresh_provider_catalogs(
                config,
                now=now,
                run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "offline"),
            )
            self.assertEqual(result.failed, ("codex",))
            loaded = cache.load("codex")
            assert loaded is not None
            self.assertEqual(tuple(item.model_id for item in loaded.models), ("old",))

    def test_unconfigured_multi_auth_replaces_legacy_matrix_with_native_catalog(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(self._config(root), codex_multi_auth_executable=None)
            cache = ProviderCatalogCache(root / "provider-model-catalogs.json")
            now = datetime(2026, 9, 4, tzinfo=timezone.utc)
            cache.store(
                "codex",
                (ProviderModel("gpt-5-mini", "GPT 5 Mini", ("medium",)),),
                source_version="codex-multi-auth 2.12.0",
                observed_at=now,
            )

            def forbidden_run(*_args, **_kwargs):
                raise AssertionError("unconfigured multi-auth must not be executed")

            native = (
                ProviderModel("native-a", "Native A", ("low", "high")),
                ProviderModel("native-b", "Native B", ("medium",)),
            )
            with patch(
                "hermes_codex_router.catalog_refresh.native_codex_models", return_value=native
            ):
                result = refresh_provider_catalogs(config, now=now, run=forbidden_run)

            self.assertEqual(result.refreshed, ("codex",))
            loaded = cache.load("codex")
            assert loaded is not None
            self.assertEqual(
                [(model.model_id, model.efforts) for model in loaded.models],
                [("native-a", ("low", "high")), ("native-b", ("medium",))],
            )
            self.assertEqual(loaded.source_version, "codex model/list openai-only")

    def test_native_failure_preserves_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(self._config(root), codex_multi_auth_executable=None)
            cache = ProviderCatalogCache(root / "provider-model-catalogs.json")
            cache.store(
                "codex",
                (ProviderModel("old", "Old", ("high",)),),
                source_version="configured fallback",
            )
            with patch(
                "hermes_codex_router.catalog_refresh.native_codex_models",
                side_effect=RuntimeError("offline"),
            ):
                result = refresh_provider_catalogs(config)
            self.assertEqual(result.failed, ("codex",))
            loaded = cache.load("codex")
            assert loaded is not None
            self.assertEqual(loaded.models[0].model_id, "old")

    def test_explicit_route_refreshes_from_tagged_native_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(self._config(root), codex_model_provider="example-route")
            cache = ProviderCatalogCache(root / "provider-model-catalogs.json")
            with (
                patch("hermes_codex_router.catalog_refresh.UnixWebSocketTransport"),
                patch("hermes_codex_router.catalog_refresh.CodexAppServerClient") as factory,
            ):
                factory.return_value.list_models.return_value = (
                    {
                        "id": "gpt-6-astra",
                        "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                    },
                    {
                        "id": "special-model",
                        "modelProvider": "example-route",
                        "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
                    },
                    {
                        "id": "foreign-model",
                        "modelProvider": "other-route",
                        "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                    },
                )
                result = refresh_provider_catalogs(config)
            self.assertEqual(result.refreshed, ("codex",))
            cached = cache.load("codex")
            assert cached is not None
            self.assertEqual(
                [model.model_id for model in cached.models], ["gpt-6-astra", "special-model"]
            )
            self.assertEqual(cached.source_version, "codex model/list route-filtered example-route")

    def test_native_refresh_replaces_fresh_unfiltered_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(self._config(root), codex_multi_auth_executable=None)
            cache = ProviderCatalogCache(root / "provider-model-catalogs.json")
            now = datetime(2026, 9, 4, tzinfo=timezone.utc)
            cache.store(
                "codex",
                (
                    ProviderModel("gpt-6-astra", "GPT", ("high",)),
                    ProviderModel("gemini-3-flash", "Gemini", ("high",)),
                ),
                source_version="codex model/list",
                observed_at=now,
            )
            with patch(
                "hermes_codex_router.catalog_refresh.native_codex_models",
                return_value=(ProviderModel("gpt-6-astra", "GPT", ("high",)),),
            ):
                result = refresh_provider_catalogs(config, now=now)
            self.assertEqual(result.refreshed, ("codex",))
            loaded = cache.load("codex")
            assert loaded is not None
            self.assertEqual([model.model_id for model in loaded.models], ["gpt-6-astra"])


if __name__ == "__main__":
    unittest.main()
