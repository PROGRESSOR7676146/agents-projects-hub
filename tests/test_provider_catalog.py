from __future__ import annotations

import subprocess
import unittest

from hermes_codex_router.provider_catalog import (
    ProviderModel,
    antigravity_models,
    codex_models,
    opencode_models,
)


class ProviderCatalogTests(unittest.TestCase):
    def test_parses_available_codex_model_matrix(self) -> None:
        output = (
            '{"matrix":{"entries":['
            '{"model":"gpt-5.6-sol","normalizedModel":"gpt-5.6-sol",'
            '"available":true,"defaultReasoningEffort":"high",'
            '"supportedReasoningEfforts":["medium","high","xhigh"]},'
            '{"model":"gpt-hidden","available":false}]}}'
        )
        models = codex_models(
            "/usr/bin/codex-multi-auth",
            run=lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, ""),
        )
        self.assertEqual(
            models,
            (ProviderModel("gpt-5.6-sol", "GPT 5.6 Sol", ("medium", "high", "xhigh")),),
        )

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
        output = """Fetching available models...
gemini-3.7-flash-high\tGemini 3.7 Flash (High)
gemini-3.7-flash-medium\tGemini 3.7 Flash (Medium)
gemini-3.7-flash-low\tGemini 3.7 Flash (Low)
claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)
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

    def test_fallback_includes_gemini_3_8_flash(self) -> None:
        from datetime import timedelta

        from hermes_codex_router.provider_catalog import ANTIGRAVITY_FALLBACK, DEFAULT_CATALOG_TTL

        self.assertEqual(DEFAULT_CATALOG_TTL, timedelta(hours=12))
        model_ids = [m.model_id for m in ANTIGRAVITY_FALLBACK]
        self.assertIn("gemini-3.8-flash", model_ids)


if __name__ == "__main__":
    unittest.main()
