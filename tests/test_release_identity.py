from __future__ import annotations

import io
import json
import tomllib
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from hermes_codex_router.cli import main
from hermes_codex_router.release_identity import CURRENT_RELEASE, ReleaseIdentity


class ReleaseIdentityTests(unittest.TestCase):
    def test_verified_identity_requires_complete_clean_provenance(self) -> None:
        identity = ReleaseIdentity(
            "0.6.0",
            "a" * 40,
            "2026-09-04T12:00:00+00:00",
            True,
        )

        self.assertTrue(identity.verified)
        self.assertFalse(ReleaseIdentity("0.6.0", None, None, False).verified)
        with self.assertRaises(ValueError):
            ReleaseIdentity("0.6.0", None, None, True)
        with self.assertRaises(ValueError):
            ReleaseIdentity("0.6.0", "not-a-sha", None, False)

    def test_source_fallback_is_explicitly_unverified(self) -> None:
        pyproject = tomllib.loads(
            (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(CURRENT_RELEASE.package_version, pyproject["project"]["version"])
        self.assertFalse(CURRENT_RELEASE.verified)

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["release-info"]), 1)
        rendered = json.loads(output.getvalue())
        self.assertEqual(rendered["package_version"], pyproject["project"]["version"])
        self.assertFalse(rendered["clean_tree"])
        self.assertNotIn("path", rendered)


if __name__ == "__main__":
    unittest.main()
