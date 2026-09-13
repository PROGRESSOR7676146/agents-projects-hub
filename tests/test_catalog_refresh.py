from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_codex_router.catalog_refresh import refresh_provider_catalogs
from hermes_codex_router.hub_config import AgentDefinition, HubConfig
from hermes_codex_router.provider_catalog import ProviderModel
from hermes_codex_router.provider_catalog_cache import ProviderCatalogCache


class CatalogRefreshTests(unittest.TestCase):
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

    def test_unconfigured_multi_auth_replaces_fresh_codex_matrix_with_configured_model(
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

            result = refresh_provider_catalogs(config, now=now, run=forbidden_run)

            self.assertEqual(result.refreshed, ("codex",))
            loaded = cache.load("codex")
            assert loaded is not None
            self.assertEqual(
                [(model.model_id, model.efforts) for model in loaded.models],
                [("gpt", ("high",))],
            )
            self.assertEqual(loaded.source_version, "configured fallback")


if __name__ == "__main__":
    unittest.main()
