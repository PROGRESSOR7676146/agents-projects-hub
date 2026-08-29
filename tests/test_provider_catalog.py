from __future__ import annotations

import subprocess
import unittest

from hermes_codex_router.provider_catalog import (
    ProviderModel,
    antigravity_models,
    opencode_models,
)


class ProviderCatalogTests(unittest.TestCase):
    def test_parses_opencode_verbose_models_and_variants(self) -> None:
        output = """opencode-go/deepseek-v4-flash
{"id":"deepseek-v4-flash","providerID":"opencode-go","name":"DeepSeek V4 Flash","variants":{"low":{"reasoningEffort":"low"},"high":{"reasoningEffort":"high"},"max":{"reasoningEffort":"max"}}}
opencode-go/kimi-k3
{"id":"kimi-k3","providerID":"opencode-go","name":"Kimi K3","variants":{}}
"""
        models = opencode_models(
            "/usr/bin/opencode",
            run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, ""),
        )
        self.assertEqual(
            models,
            (
                ProviderModel(
                    "opencode-go/deepseek-v4-flash",
                    "DeepSeek V4 Flash",
                    ("low", "high", "max"),
                ),
                ProviderModel("opencode-go/kimi-k3", "Kimi K3", ("default",)),
            ),
        )

    def test_groups_antigravity_effort_suffixes(self) -> None:
        output = """gemini-3.7-flash-high
gemini-3.7-flash-medium
gemini-3.7-flash-low
claude-sonnet-4-6
"""
        models = antigravity_models(
            "/usr/bin/agy",
            run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, ""),
        )
        self.assertEqual(
            models,
            (
                ProviderModel(
                    "gemini-3.7-flash",
                    "Gemini 3.7 Flash",
                    ("high", "medium", "low"),
                ),
                ProviderModel("claude-sonnet-4-6", "Claude Sonnet 4 6", ("default",)),
            ),
        )


if __name__ == "__main__":
    unittest.main()
