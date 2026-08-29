from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_codex_router.provider_catalog import ProviderModel
from hermes_codex_router.provider_catalog_cache import ProviderCatalogCache


class ProviderCatalogCacheTests(unittest.TestCase):
    def test_round_trip_is_private_atomic_and_uses_short_stable_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private" / "model-catalogs.json"
            cache = ProviderCatalogCache(path)
            models = (
                ProviderModel(
                    "opencode-go/deepseek-v4-flash-vision-exp",
                    "DeepSeek V4 Flash Vision Exp",
                    ("low", "high", "max"),
                ),
            )
            stored = cache.store("opencode", models, source_version="1.2.3")
            loaded = cache.load("opencode")

            assert loaded is not None
            self.assertEqual(loaded, stored)
            self.assertEqual(loaded.models[0].model_id, models[0].model_id)
            self.assertEqual(loaded.models[0].efforts, models[0].efforts)
            self.assertEqual(loaded.source_version, "1.2.3")
            self.assertLessEqual(len(loaded.models[0].callback_key), 12)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)

    def test_failure_only_becomes_stale_after_max_age(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model-catalogs.json"
            cache = ProviderCatalogCache(path)
            now = datetime(2026, 8, 30, tzinfo=timezone.utc)
            cache.store(
                "antigravity",
                (ProviderModel("gemini", "Gemini", ("high",)),),
                source_version="1",
                observed_at=now - timedelta(hours=25),
            )
            cache.mark_failure("antigravity", observed_at=now)

            self.assertEqual(
                cache.stale_agents(now=now, max_age=timedelta(hours=24)), ("antigravity",)
            )

    def test_rejects_tampered_or_colliding_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model-catalogs.json"
            path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
            os.chmod(path, 0o600)
            self.assertIsNone(ProviderCatalogCache(path).load("codex"))


if __name__ == "__main__":
    unittest.main()
