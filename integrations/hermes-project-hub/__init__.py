"""Hermes user-plugin entry point for Hub."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_SOURCE_ROOT = os.getenv("HERMES_PROJECT_HUB_SOURCE")
if not _SOURCE_ROOT:
    _SOURCE_ROOT = str(Path(__file__).resolve().parents[2] / "src")
if _SOURCE_ROOT not in sys.path:
    sys.path.insert(0, _SOURCE_ROOT)

from hermes_codex_router.hermes_plugin import register  # noqa: E402,F401
