from __future__ import annotations

import unittest

from hermes_codex_router.model_selection import (
    ModelSelectionError,
    available_models,
    available_openai_models,
    require_model_effort,
)

MODELS = [
    {
        "id": "gpt-5.6-sol",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low"},
            {"reasoningEffort": "high"},
            {"reasoningEffort": "ultra"},
        ],
    },
    {"id": "broken"},
]


class ModelSelectionTests(unittest.TestCase):
    def test_openai_catalog_rejects_other_provider_models_without_losing_gpt_and_o_series(
        self,
    ) -> None:
        models = [
            {"id": "gpt-6-astra", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
            {"id": "claude-sonnet-4-6", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
            {"id": "gemini-3-flash", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
            {"id": "o3", "supportedReasoningEfforts": [{"reasoningEffort": "medium"}]},
        ]
        self.assertEqual(
            available_openai_models(models),
            {"gpt-6-astra": ("high",), "o3": ("medium",)},
        )

    def test_custom_route_does_not_admit_foreign_models(self) -> None:
        entries = [
            {"id": "gpt-6-astra", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
            {
                "id": "special-model",
                "modelProvider": "example-route",
                "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
            },
            {"id": "claude-sonnet-4-6", "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
            {
                "id": "gemini-3-flash",
                "modelProvider": "example-route",
                "supportedReasoningEfforts": [{"reasoningEffort": "low"}],
            },
            {
                "id": "gpt-5.6-sol",
                "modelProvider": "example-route",
                "supportedReasoningEfforts": [{"reasoningEffort": "medium"}],
            },
        ]
        self.assertEqual(
            available_openai_models(entries, model_provider="example-route"),
            {"gpt-6-astra": ("high",), "gpt-5.6-sol": ("medium",)},
        )

    def test_extracts_only_models_with_efforts(self) -> None:
        self.assertEqual(
            available_models(MODELS),
            {"gpt-5.6-sol": ("low", "high", "ultra")},
        )

    def test_requires_live_supported_pair(self) -> None:
        self.assertEqual(
            require_model_effort(MODELS, "gpt-5.6-sol", "high"),
            ("gpt-5.6-sol", "high"),
        )
        with self.assertRaises(ModelSelectionError):
            require_model_effort(MODELS, "gpt-5.6-sol", "max")


if __name__ == "__main__":
    unittest.main()
