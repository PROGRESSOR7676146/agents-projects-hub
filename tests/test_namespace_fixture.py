"""The hosted namespace gate must turn fixture absence into a test failure."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests.namespace_fixture import namespace_unavailable


class NamespaceFixtureTests(unittest.TestCase):
    def test_default_developer_environment_skips_unavailable_fixture(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(unittest.SkipTest, "example namespace unavailable"):
                namespace_unavailable(self, "example namespace unavailable")

    def test_required_ci_environment_fails_unavailable_fixture(self) -> None:
        with patch.dict(os.environ, {"HUB_REQUIRE_NAMESPACE_TESTS": "1"}, clear=True):
            with self.assertRaisesRegex(AssertionError, "required namespace fixture unavailable"):
                namespace_unavailable(self, "example namespace unavailable")
