from __future__ import annotations

import unittest

from hermes_codex_router.model_selection import (
    ModelSelectionError,
    available_models,
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
