from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from hermes_codex_router import claude_catalog
from hermes_codex_router.claude_catalog import (
    CONFIGURED_SOURCE,
    ROUTE_SOURCE,
    cached_claude_snapshot,
    controller_claude_snapshot,
    fetch_route_model_rows,
    route_claude_models,
    route_claude_snapshot,
)
from hermes_codex_router.claude_permissions_config import parse_claude_file_permissions
from hermes_codex_router.provider_catalog import ProviderCatalogError, ProviderModel
from hermes_codex_router.provider_catalog_cache import ProviderCatalogCache

ROWS = [
    {"id": "claude-opus-5-5", "owned_by": "anthropic", "display_name": "Claude Opus 5.5"},
    {"id": "claude-opus-5", "owned_by": "anthropic", "display_name": "Claude Opus 5"},
    {"id": "claude-opus-4-1-20250805", "owned_by": "anthropic", "display_name": "Claude 4.1 Opus"},
    {"id": "claude-sonnet-5-5", "owned_by": "anthropic", "display_name": "Claude Sonnet 5.5"},
    {"id": "claude-haiku-5-5", "owned_by": "anthropic", "display_name": "Claude Haiku 5.5"},
    {"id": "claude-3-5-haiku-20241022", "owned_by": "anthropic", "display_name": "Old"},
    {"id": "claude-fable-5-1", "owned_by": "anthropic", "display_name": "Claude Fable 5.1"},
    # Proxy aliases that route a Claude client to other providers.
    {"id": "claude-opus-5-5-high", "owned_by": "antigravity", "display_name": "Opus (High)"},
    {"id": "claude-fable-5-dd-example", "owned_by": "openai", "display_name": "GPT Example"},
    {"id": "gemini-example", "owned_by": "google"},
]


def agent(**changes: object) -> Any:
    values = {
        "agent_id": "claude",
        "default_model": "claude-opus-5-5",
        "default_effort": "high",
        "model_catalog": None,
        "auth_route": "cpa",
        "runtime": "claude",
        "managed_externally": False,
    }
    values.update(changes)
    return SimpleNamespace(**values)


class ClaudeRouteCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.cache = ProviderCatalogCache(self.base / "provider-model-catalogs.json")
        self.permissions = parse_claude_file_permissions(
            {
                "profile": "host",
                "provider_home": str(self.base / "sessions"),
                "route_environment": str(self.base / "route.environment"),
            }
        )

    def test_newest_anthropic_model_per_family_and_no_foreign_aliases(self) -> None:
        models = route_claude_models(ROWS, default_model="claude-opus-5-5", default_effort="high")
        self.assertEqual(
            [model.model_id for model in models],
            ["claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"],
        )
        self.assertEqual(models[1].label, "Claude Opus 5.5")
        self.assertEqual(models[1].efforts, ("low", "medium", "high", "xhigh", "max"))
        self.assertEqual(models[3].efforts, ("high",))

    def test_default_is_always_offered(self) -> None:
        models = route_claude_models(
            ROWS[:1], default_model="claude-example-pinned", default_effort="max"
        )
        self.assertEqual(
            models[-1], ProviderModel("claude-example-pinned", "claude-example-pinned", ("max",))
        )
        self.assertEqual(
            [
                model.model_id
                for model in route_claude_models([], default_model="d", default_effort="high")
            ],
            ["d"],
        )

    def test_controller_uses_a_saved_route_catalog_only_when_it_covers_the_default(self) -> None:
        claude = agent()
        p = self.permissions
        self.assertIsNone(cached_claude_snapshot(self.cache, claude, p))
        snapshot = route_claude_snapshot(self.cache, claude, ROWS)
        self.assertEqual(snapshot.source_version, ROUTE_SOURCE)
        self.assertEqual(cached_claude_snapshot(self.cache, claude, p), snapshot)
        self.assertIsNone(cached_claude_snapshot(self.cache, agent(default_effort="turbo"), p))
        explicit = agent(model_catalog=(ProviderModel("claude-opus-5-5", "x", ("high",)),))
        self.assertIsNone(cached_claude_snapshot(self.cache, explicit, p))
        # Configuration no longer routes this agent's catalog: the saved route
        # catalog is not shown even before the monitor reconciles it.
        self.assertIsNone(cached_claude_snapshot(self.cache, agent(auth_route="subscription"), p))
        self.assertIsNone(cached_claude_snapshot(self.cache, claude, None))

    def test_foreign_default_dated_versions_and_two_digit_minors(self) -> None:
        with self.assertRaises(ProviderCatalogError):
            route_claude_models(ROWS, default_model="claude-opus-5-5-high", default_effort="high")
        rows = [
            {"id": "claude-sonnet-5-5-20261101", "owned_by": "anthropic"},
            {"id": "claude-sonnet-5-5-20261001", "owned_by": "anthropic"},
            {"id": "claude-sonnet-5-10", "owned_by": "anthropic"},
            {"id": "claude-opus-5-5-20261001", "owned_by": "anthropic"},
            {"id": "claude-opus-5-5", "owned_by": "anthropic"},
        ]
        for order in (rows, list(reversed(rows))):
            ids = [
                m.model_id
                for m in route_claude_models(order, default_model="d", default_effort="high")
            ]
            self.assertEqual(ids, ["claude-opus-5-5", "claude-sonnet-5-10", "d"])

    def test_controller_fallback_never_replaces_a_route_catalog_stored_meanwhile(self) -> None:
        claude = agent()
        real = claude_catalog.cached_claude_snapshot
        calls = [0]

        def interleaved(cache, agent_, permissions):
            calls[0] += 1
            if calls[0] == 1:
                # The monitor stores the route catalog right after the first read.
                route_claude_snapshot(cache, agent_, ROWS)
                return None
            return real(cache, agent_, permissions)

        with patch.object(claude_catalog, "cached_claude_snapshot", side_effect=interleaved):
            shown = controller_claude_snapshot(self.cache, claude, self.permissions)
        self.assertEqual(shown.source_version, ROUTE_SOURCE)
        saved = self.cache.load("claude")
        assert saved is not None
        self.assertEqual(saved.source_version, ROUTE_SOURCE)

    def test_fetch_errors_and_slow_responses_become_catalog_failures(self) -> None:
        import http.client

        route = self.base / "route.environment"
        route.write_text("ANTHROPIC_BASE_URL=http://127.0.0.1:8317\nANTHROPIC_AUTH_TOKEN=example\n")
        route.chmod(0o600)

        class Response:
            def __init__(self, chunks):
                self.chunks = list(chunks)

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _size):
                if self.chunks and isinstance(self.chunks[0], Exception):
                    raise self.chunks.pop(0)
                return self.chunks.pop(0) if self.chunks else b""

        def opener(response):
            return SimpleNamespace(open=lambda request, timeout: response)

        with patch(
            "urllib.request.build_opener",
            return_value=opener(Response([http.client.IncompleteRead(b"x")])),
        ):
            with self.assertRaises(ProviderCatalogError):
                fetch_route_model_rows(route)
        clock = iter([0.0, 1.0, 20.0, 40.0])
        with (
            patch("urllib.request.build_opener", return_value=opener(Response([b"{", b"}", b" "]))),
            patch.object(claude_catalog.time, "monotonic", side_effect=lambda: next(clock)),
        ):
            with self.assertRaises(ProviderCatalogError):
                fetch_route_model_rows(route)

    def refresh(self, claude: Any, **patches: Any):
        from hermes_codex_router.catalog_refresh import refresh_provider_catalogs

        route = self.base / "route.environment"
        config = SimpleNamespace(
            state_path=self.base / "state.db",
            agents=(claude,),
            claude_file_permissions=parse_claude_file_permissions(
                {
                    "profile": "host",
                    "provider_home": str(self.base / "sessions"),
                    "route_environment": str(route),
                }
            ),
            codex_model_provider=None,
        )
        with patch(
            "hermes_codex_router.catalog_refresh.fetch_route_model_rows", **patches
        ) as fetch:
            result = refresh_provider_catalogs(cast(Any, config), now=datetime.now(timezone.utc))
        return result, fetch

    def test_monitor_refresh_stores_the_route_catalog_and_keeps_it_on_failure(self) -> None:
        claude = agent()
        result, fetch = self.refresh(claude, return_value=ROWS)
        fetch.assert_called_once()
        saved = self.cache.load("claude")
        assert saved is not None
        self.assertEqual(saved.source_version, ROUTE_SOURCE)
        self.assertEqual(len(saved.models), 4)
        # Fresh: no second fetch within the catalog lifetime.
        _, fetch = self.refresh(claude, side_effect=AssertionError("fetched while fresh"))
        fetch.assert_not_called()
        # Stale and failing: the last good catalog stays.
        with patch.object(ProviderCatalogCache, "is_stale", return_value=True):
            result, fetch = self.refresh(claude, side_effect=OSError("route down"))
        fetch.assert_called_once()
        self.assertIn("claude", result.failed)
        self.assertEqual(self.cache.load("claude"), saved)

    def test_explicit_catalog_still_wins_without_any_route_request(self) -> None:
        models = (ProviderModel("claude-opus-5-5", "Opus", ("high",)),)
        _, fetch = self.refresh(
            agent(model_catalog=models), side_effect=AssertionError("route fetched")
        )
        fetch.assert_not_called()
        saved = self.cache.load("claude")
        assert saved is not None
        self.assertEqual(saved.source_version, CONFIGURED_SOURCE)


if __name__ == "__main__":
    unittest.main()
