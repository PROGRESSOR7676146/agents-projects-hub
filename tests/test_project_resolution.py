from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hermes_codex_router.hub_config import HubConfig, ProjectBinding, TerminalSettings
from hermes_codex_router.project_resolution import (
    ProjectResolutionError,
    list_resolved_project_groups,
    resolve_project_context,
    resolve_project_group,
)
from hermes_codex_router.state import HubState


class ProjectResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.allowed = self.base / "projects"
        self.allowed.mkdir()
        self.root = self.allowed / "example"
        self.root.mkdir()
        subprocess.run(("git", "init", "-q", str(self.root)), check=True)
        self.registry_path = self.base / "registry.json"
        self._write_registry(self.root, enabled=True)
        self.config = HubConfig(
            schema_version=1,
            owner_user_ids=(42,),
            registry_path=self.registry_path,
            state_path=self.base / "state.db",
            codex_socket_path=self.base / "codex.sock",
            manage_codex_server=False,
            terminal=TerminalSettings("tmux-only", None, "Ubuntu"),
            projects=(ProjectBinding("example", -1001234567890),),
            agents=(),
        )
        self.state = HubState.open(self.config.state_path)
        self.addCleanup(self.state.close)

    def _write_registry(self, root: Path, *, enabled: bool) -> None:
        self.registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_roots": [str(self.allowed)],
                    "projects": [
                        {
                            "project_id": "example",
                            "display_name": "Example",
                            "topic_name": "Example",
                            "root": str(root),
                            "enabled": enabled,
                        }
                    ],
                }
            )
        )

    def _insert_receipt(self, chat_id: int, *, canonical_root: Path | None = None) -> None:
        root = canonical_root or self.root
        now = datetime.now(timezone.utc)
        self.state._connection.execute(
            """INSERT INTO project_onboarding_workflows
               (workflow_id,owner_user_id,display_name,project_id,base_root,canonical_root,
                stage,expires_at,created_at,updated_at,required_owner_ids_json)
               VALUES ('dynamic-workflow',42,'Example','example',?,?,'completed',?,?,?,'[42]')""",
            (
                str(self.allowed),
                str(root),
                (now + timedelta(hours=1)).isoformat(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        self.state._connection.execute(
            """INSERT INTO project_group_bindings
               (project_id,telegram_chat_id,canonical_root,workflow_id,created_at)
               VALUES ('example',?,?,'dynamic-workflow',?)""",
            (chat_id, str(root), now.isoformat()),
        )
        self.state._connection.commit()

    def test_static_binding_resolves_current_git_project(self) -> None:
        resolved = resolve_project_context(
            self.config,
            self.state,
            chat_id=-1001234567890,
            expected_project_id="example",
            expected_root=self.root,
        )
        self.assertEqual(resolved.project.root, self.root)
        self.assertEqual(resolved.source, "static")
        self.assertEqual(len(list_resolved_project_groups(self.config, self.state)), 1)

    def test_missing_disabled_and_wrong_expected_binding_fail_closed(self) -> None:
        with self.assertRaisesRegex(ProjectResolutionError, "project_binding_missing"):
            resolve_project_context(self.config, self.state, chat_id=-1009999999999)
        with self.assertRaisesRegex(ProjectResolutionError, "project_binding_mismatch"):
            resolve_project_context(
                self.config,
                self.state,
                chat_id=-1001234567890,
                expected_project_id="other",
            )
        self._write_registry(self.root, enabled=False)
        with self.assertRaisesRegex(ProjectResolutionError, "project_binding_invalid"):
            resolve_project_context(self.config, self.state, chat_id=-1001234567890)

    def test_dynamic_receipt_resolves_and_rejects_registry_root_replacement(self) -> None:
        chat_id = -1002222222222
        dynamic_config = replace(self.config, projects=())
        self._insert_receipt(chat_id)
        resolved = resolve_project_context(dynamic_config, self.state, chat_id=chat_id)
        self.assertEqual((resolved.source, resolved.project.root), ("onboarding", self.root))

        replacement = self.allowed / "replacement"
        replacement.mkdir()
        subprocess.run(("git", "init", "-q", str(replacement)), check=True)
        self._write_registry(replacement, enabled=True)
        with self.assertRaisesRegex(ProjectResolutionError, "project_binding_mismatch"):
            resolve_project_context(dynamic_config, self.state, chat_id=chat_id)

    def test_matching_static_and_dynamic_receipt_is_accepted_once(self) -> None:
        self._insert_receipt(-1001234567890)
        resolved = list_resolved_project_groups(self.config, self.state)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].source, "static+onboarding")

    def test_contradictory_static_and_dynamic_binding_fails_closed(self) -> None:
        self._insert_receipt(-1001234567890)
        self.state._connection.execute(
            "UPDATE project_group_bindings SET project_id='other' WHERE workflow_id='dynamic-workflow'"
        )
        self.state._connection.commit()
        issues = []
        self.assertEqual(list_resolved_project_groups(self.config, self.state, issues=issues), ())
        self.assertEqual([item.error_code for item in issues], ["project_binding_conflict"])

    def test_direct_resolution_requires_exact_owner_and_configured_project(self) -> None:
        config = replace(self.config, direct_message_project_id="example")
        resolved = resolve_project_context(
            config,
            self.state,
            chat_id=42,
            expected_project_id="example",
        )
        self.assertEqual((resolved.project.project_id, resolved.source), ("example", "direct"))
        for chat_id, project_id in ((43, "example"), (42, "other")):
            with self.subTest(chat_id=chat_id, project_id=project_id):
                with self.assertRaises(ProjectResolutionError):
                    resolve_project_context(
                        config,
                        self.state,
                        chat_id=chat_id,
                        expected_project_id=project_id,
                    )

    def test_exact_group_resolution_ignores_unrelated_disabled_project(self) -> None:
        other = self.allowed / "disabled"
        other.mkdir()
        subprocess.run(("git", "init", "-q", str(other)), check=True)
        document = json.loads(self.registry_path.read_text())
        document["projects"].append(
            {
                "project_id": "disabled",
                "display_name": "Disabled",
                "topic_name": "Disabled",
                "root": str(other),
                "enabled": False,
            }
        )
        self.registry_path.write_text(json.dumps(document))
        config = replace(
            self.config,
            projects=self.config.projects + (ProjectBinding("disabled", -1002222222222),),
        )
        resolved = resolve_project_group(config, self.state, project_id="example")
        self.assertEqual(resolved.project.project_id, "example")
        issues = []
        listed = list_resolved_project_groups(config, self.state, issues=issues)
        self.assertEqual([item.project.project_id for item in listed], ["example"])
        self.assertEqual([item.chat_id for item in issues], [-1002222222222])

    def test_symlink_escape_is_rejected(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        subprocess.run(("git", "init", "-q", str(outside)), check=True)
        link = self.allowed / "escape"
        link.symlink_to(outside, target_is_directory=True)
        self._write_registry(link, enabled=True)
        with self.assertRaisesRegex(ProjectResolutionError, "project_binding_invalid"):
            resolve_project_context(self.config, self.state, chat_id=-1001234567890)
