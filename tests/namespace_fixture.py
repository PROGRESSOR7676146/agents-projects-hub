"""Developer-friendly namespace fixture availability with a strict CI mode."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from typing import NoReturn

from hermes_codex_router.claude_file_sandbox import FileToolSandboxError, _require_fd_bind_support


def namespace_unavailable(case: unittest.TestCase, reason: str) -> NoReturn:
    """Skip locally, but fail a required namespace check when its setup is absent."""
    if os.environ.get("HUB_REQUIRE_NAMESPACE_TESTS") == "1":
        case.fail(f"required namespace fixture unavailable: {reason}")
    case.skipTest(reason)


def require_namespace_runtime(case: unittest.TestCase, executable: Path) -> None:
    """Keep builder mocks separate from the actual required bwrap capability."""
    try:
        _require_fd_bind_support(executable)
    except FileToolSandboxError:
        namespace_unavailable(case, "bubblewrap descriptor binds unavailable")


def namespace_permission_refused(stderr: str) -> bool:
    """Recognize only known kernel/AppArmor fixture refusals, not launch defects."""
    return any(
        message in stderr
        for message in (
            "Creating new namespace failed",
            "No permissions to creating new namespace",
            "setting up uid map: Permission denied",
        )
    )
