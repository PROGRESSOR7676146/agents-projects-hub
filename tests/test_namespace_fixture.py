"""The hosted namespace gate must turn fixture absence into a test failure."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.claude_file_sandbox import FileToolSandboxError
from tests.namespace_fixture import (
    namespace_permission_refused,
    namespace_unavailable,
    require_namespace_runtime,
)


class NamespaceFixtureTests(unittest.TestCase):
    def test_older_runtime_skips_locally_but_fails_in_required_job(self) -> None:
        with patch(
            "tests.namespace_fixture._require_fd_bind_support",
            side_effect=FileToolSandboxError("example older build"),
        ):
            for strict, expected in (("0", unittest.SkipTest), ("1", AssertionError)):
                with patch.dict(os.environ, {"HUB_REQUIRE_NAMESPACE_TESTS": strict}):
                    with self.assertRaises(expected):
                        require_namespace_runtime(self, Path("/usr/bin/bwrap"))

    def test_only_known_kernel_refusals_are_unavailable(self) -> None:
        for message in (
            "Creating new namespace failed: Operation not permitted",
            "No permissions to creating new namespace, likely because the kernel does not allow it",
            "bwrap: setting up uid map: Permission denied",
        ):
            self.assertTrue(namespace_permission_refused(message))
        for message in ("Race condition binding dirfd", "Can't bind mount", "example child failed"):
            self.assertFalse(namespace_permission_refused(message))

    def test_default_developer_environment_skips_unavailable_fixture(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(unittest.SkipTest, "example namespace unavailable"):
                namespace_unavailable(self, "example namespace unavailable")

    def test_required_ci_environment_fails_unavailable_fixture(self) -> None:
        with patch.dict(os.environ, {"HUB_REQUIRE_NAMESPACE_TESTS": "1"}, clear=True):
            with self.assertRaisesRegex(AssertionError, "required namespace fixture unavailable"):
                namespace_unavailable(self, "example namespace unavailable")
