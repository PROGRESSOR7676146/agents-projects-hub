from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.release_metadata import audit_release_metadata


class ReleaseMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "docs" / "status").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_metadata(
        self,
        *,
        package: str = "1.2.3",
        status: str = "1.2.3",
        changelog: tuple[str, ...] = ("1.2.3", "1.2.2"),
    ) -> None:
        (self.root / "pyproject.toml").write_text(
            f'[project]\nname = "example"\nversion = "{package}"\n', encoding="utf-8"
        )
        (self.root / "docs" / "status" / "PROJECT_STATUS.md").write_text(
            f"# Project status\n\nRelease: v{status}\n", encoding="utf-8"
        )
        entries = "\n\n".join(f"## [{version}] - 2026-09-05" for version in changelog)
        (self.root / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## [Unreleased]\n\n{entries}\n", encoding="utf-8"
        )

    def test_missing_current_tag_is_visible_debt_not_a_branch_failure(self) -> None:
        self.write_metadata()

        result = audit_release_metadata(self.root, tags=("v1.2.2",), head_tags=())

        self.assertEqual(result.version, "1.2.3")
        self.assertEqual(result.errors, ())
        self.assertEqual(result.debts, ("missing release tag: v1.2.3",))

    def test_package_status_and_latest_changelog_must_match(self) -> None:
        self.write_metadata(status="1.2.2", changelog=("1.2.1", "1.2.0"))

        result = audit_release_metadata(self.root, tags=(), head_tags=())

        self.assertEqual(
            result.errors,
            (
                "project status release v1.2.2 does not match package version 1.2.3",
                "latest changelog release 1.2.1 does not match package version 1.2.3",
            ),
        )

    def test_version_tag_requires_matching_changelog_entry(self) -> None:
        self.write_metadata()

        result = audit_release_metadata(self.root, tags=("v1.2.3", "v9.9.9"), head_tags=("v1.2.3",))

        self.assertEqual(result.errors, ("release tag v9.9.9 has no changelog entry",))
        self.assertEqual(result.debts, ("missing release tag: v1.2.2",))

    def test_current_head_version_tag_must_match_package(self) -> None:
        self.write_metadata()

        result = audit_release_metadata(self.root, tags=("v1.2.2",), head_tags=("v1.2.2",))

        self.assertEqual(
            result.errors,
            ("HEAD release tag v1.2.2 does not match package version v1.2.3",),
        )

    def test_duplicate_changelog_release_is_rejected(self) -> None:
        self.write_metadata(changelog=("1.2.3", "1.2.3"))

        result = audit_release_metadata(self.root, tags=("v1.2.3",), head_tags=())

        self.assertEqual(result.errors, ("duplicate changelog release: 1.2.3",))

    def test_semver_and_required_unreleased_section_are_enforced(self) -> None:
        self.write_metadata(package="release-1", status="release-1", changelog=("1.2.3",))
        (self.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## [1.2.3] - 2026-09-05\n", encoding="utf-8"
        )

        result = audit_release_metadata(self.root, tags=(), head_tags=())

        self.assertIn("package version is not canonical SemVer: release-1", result.errors)
        self.assertIn("changelog must begin with an Unreleased section", result.errors)


if __name__ == "__main__":
    unittest.main()
