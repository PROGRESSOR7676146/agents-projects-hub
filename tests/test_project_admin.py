from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.project_admin import add_project, set_project_enabled
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


if __name__ == "__main__":
    unittest.main()
