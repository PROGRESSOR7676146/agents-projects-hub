from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.registry import RegistryError, build_codex_argv, load_registry


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.project = self.base / "Project One"
        self.project.mkdir()
        (self.project / ".git").mkdir()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_registry(self, **project_overrides: object) -> Path:
        project = {
            "project_id": "project_one",
            "display_name": "Project One",
            "topic_name": "Project One",
            "root": str(self.project),
            "sandbox": "workspace-write",
            "approval_policy": "on-request",
            "enabled": True,
        }
        project.update(project_overrides)
        path = self.base / "projects.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_roots": [str(self.base)],
                    "projects": [project],
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_loads_allowlisted_git_project(self) -> None:
        registry = load_registry(self.write_registry())
        project = registry.require_project("project_one")
        self.assertEqual(project.root, self.project.resolve())

    def test_rejects_project_outside_allowed_root(self) -> None:
        other = Path(tempfile.mkdtemp())
        (other / ".git").mkdir()
        with self.assertRaisesRegex(RegistryError, "outside allowed_roots"):
            load_registry(self.write_registry(root=str(other)))

    def test_rejects_danger_full_access(self) -> None:
        with self.assertRaisesRegex(RegistryError, "unsafe or unsupported sandbox"):
            load_registry(self.write_registry(sandbox="danger-full-access"))

    def test_rejects_never_approval_policy(self) -> None:
        with self.assertRaisesRegex(RegistryError, "unsafe or unsupported approval policy"):
            load_registry(self.write_registry(approval_policy="never"))

    def test_disabled_project_cannot_be_selected(self) -> None:
        registry = load_registry(self.write_registry(enabled=False))
        with self.assertRaises(KeyError):
            registry.require_project("project_one")

    def test_builds_shell_free_argv_with_space_in_path(self) -> None:
        project = load_registry(self.write_registry()).require_project("project_one")
        argv = build_codex_argv(project)
        self.assertEqual(argv[0], "codex")
        self.assertIn(str(self.project.resolve()), argv)
        self.assertNotIn(" ".join(argv), argv)

    def test_resume_rejects_shell_metacharacters(self) -> None:
        project = load_registry(self.write_registry()).require_project("project_one")
        with self.assertRaisesRegex(RegistryError, "invalid Codex session id"):
            build_codex_argv(project, session_id="abc; touch /tmp/no")


if __name__ == "__main__":
    unittest.main()
