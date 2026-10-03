"""Developer-friendly namespace fixture availability with a strict CI mode."""

from __future__ import annotations

import os
import unittest
from typing import NoReturn


def namespace_unavailable(case: unittest.TestCase, reason: str) -> NoReturn:
    """Skip locally, but fail a required namespace check when its setup is absent."""
    if os.environ.get("HUB_REQUIRE_NAMESPACE_TESTS") == "1":
        case.fail(f"required namespace fixture unavailable: {reason}")
    case.skipTest(reason)
