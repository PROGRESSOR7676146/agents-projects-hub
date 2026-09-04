from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
