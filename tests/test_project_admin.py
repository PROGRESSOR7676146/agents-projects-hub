from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.project_admin import (
    add_project,
    prepare_project_root,
    set_project_enabled,
)
from hermes_codex_router.registry import RegistryError, load_registry


class ProjectAdminTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.allowed = self.base / "projects"
        self.allowed.mkdir()
        self.registry = self.base / "projects.json"
        self.registry.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_roots": [str(self.allowed)],
                    "projects": [],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_add_and_disable_project_locally(self) -> None:
        root = self.allowed / "Example Project Alpha"
        (root / ".git").mkdir(parents=True)
        add_project(
            self.registry,
            project_id="alpha",
            display_name="Example Project Alpha",
            topic_name="Example Project Alpha",
            root=root,
        )
        self.assertTrue(load_registry(self.registry).require_project("alpha").enabled)
        set_project_enabled(self.registry, "alpha", False)
        with self.assertRaises(KeyError):
            load_registry(self.registry).require_project("alpha")

    def test_rejects_root_outside_existing_allowlist(self) -> None:
        root = self.base / "outside"
        (root / ".git").mkdir(parents=True)
        with self.assertRaisesRegex(RegistryError, "outside"):
            add_project(
                self.registry,
                project_id="outside",
                display_name="Outside",
                topic_name="Outside",
                root=root,
            )

    def test_prepares_only_empty_or_existing_git_direct_child(self) -> None:
        created = prepare_project_root(self.allowed, "new-project")
        self.assertEqual(created, self.allowed / "new-project")
        self.assertTrue((created / ".git").exists())
        self.assertEqual(prepare_project_root(self.allowed, "new-project"), created)

        unsafe = self.allowed / "nonempty"
        unsafe.mkdir()
        (unsafe / "data.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(RegistryError, "non-empty"):
            prepare_project_root(self.allowed, "nonempty")
        self.assertEqual((unsafe / "data.txt").read_text(encoding="utf-8"), "keep")


if __name__ == "__main__":
    unittest.main()
