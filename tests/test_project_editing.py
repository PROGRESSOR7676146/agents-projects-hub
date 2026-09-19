from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

from hermes_codex_router.hub_config import HubConfig, ProjectBinding, TerminalSettings
from hermes_codex_router.models import Project, ProjectRegistry
from hermes_codex_router.project_editing import (
    MAX_ROOT_OPTIONS,
    ProjectEditStore,
    validate_relocation_target,
)
from hermes_codex_router.project_resolution import resolve_project_context
from hermes_codex_router.registry import RegistryError, load_registry
from hermes_codex_router.state import HubState


class SimulatedCrash(BaseException):
    pass


class ProjectEditingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.allowed_a = self.base / "allowed-a"
        self.allowed_b = self.base / "allowed-b"
        self.allowed_a.mkdir()
        self.allowed_b.mkdir()
        self.old_root = self.allowed_a / "example"
        self.old_root.mkdir()
        subprocess.run(("git", "init", "-q", str(self.old_root)), check=True)
        (self.old_root / "keep.txt").write_text("preserve", encoding="utf-8")
        self.registry_path = self.base / "projects.json"
        self.registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_roots": [str(self.allowed_a), str(self.allowed_b)],
                    "projects": [
                        {
                            "project_id": "example",
                            "display_name": "Example Project",
                            "topic_name": "Telegram Group Title",
                            "root": str(self.old_root),
                            "sandbox": "workspace-write",
                            "approval_policy": "on-request",
                            "enabled": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
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
        self.addCleanup(lambda: self.state.close())

    def _start(self) -> tuple[ProjectEditStore, str]:
        store = ProjectEditStore(self.state, self.registry_path)
        workflow = store.start(owner_user_id=42, project_ids=("example",))
        option = store.project_options(workflow.workflow_id)[0]
        workflow = store.select_project(42, option.option_id)
        self.assertEqual(workflow.project_id, "example")
        return store, workflow.workflow_id

    def _dynamic_binding(self) -> None:
        now = "2026-01-01T00:00:00+00:00"
        with self.state._immediate_transaction():
            self.state._connection.execute(
                """INSERT INTO project_onboarding_workflows
                   (workflow_id,owner_user_id,display_name,project_id,base_root,
                    canonical_root,stage,telegram_chat_id,telegram_access_hash,
                    expires_at,created_at,updated_at,required_owner_ids_json)
                   VALUES ('onboarding-example',42,'Example Project','example',?,?,
                           'completed',-1001234567890,123,?,?,?,'[42]')""",
                (
                    str(self.allowed_a),
                    str(self.old_root),
                    "2099-01-01T00:00:00+00:00",
                    now,
                    now,
                ),
            )
            self.state._connection.execute(
                """INSERT INTO project_group_bindings
                   (project_id,telegram_chat_id,canonical_root,workflow_id,created_at)
                   VALUES ('example',-1001234567890,?,'onboarding-example',?)""",
                (str(self.old_root), now),
            )

    def _rename_ready(self, value: str = "Renamed Project") -> tuple[ProjectEditStore, str]:
        store, workflow_id = self._start()
        store.choose_rename(42, workflow_id)
        store.set_name(42, workflow_id, value)
        return store, workflow_id

    def _relocation_ready(self, target: Path) -> tuple[ProjectEditStore, str]:
        store, workflow_id = self._start()
        store.choose_relocation(42, workflow_id)
        option = next(item for item in store.root_options(workflow_id) if item.root == target)
        store.select_root(42, option.option_id)
        return store, workflow_id

    def test_relocation_commits_execution_scope_with_registry_and_binding(self) -> None:
        from hermes_codex_router.topic_execution import resolve_topic_execution_root

        self._dynamic_binding()
        topic = self.state.observe_topic(
            project_id="example",
            chat_id=-1001234567890,
            thread_id=7,
            title="Example",
            execution_root=self.old_root,
        )
        target = self.allowed_b / "example"
        store, workflow_id = self._relocation_ready(target)
        store.confirm(42, workflow_id)
        store.apply(workflow_id)
        refreshed = self.state.get_topic(topic.topic_id)
        self.assertEqual(refreshed.execution_scope, f"root:{target}")
        self.assertEqual(
            resolve_topic_execution_root(self.state, load_registry(self.registry_path), refreshed),
            target,
        )
        self.state.observe_topic(
            project_id="example",
            chat_id=-1001234567890,
            thread_id=7,
            title="Example",
            execution_root=target,
        )

    def test_relocation_crash_recovers_execution_scope_with_registry(self) -> None:
        self._dynamic_binding()
        topic = self.state.observe_topic(
            project_id="example",
            chat_id=-1001234567890,
            thread_id=7,
            title="Example",
            execution_root=self.old_root,
        )
        target = self.allowed_b / "example"
        store, workflow_id = self._relocation_ready(target)
        store.confirm(42, workflow_id)
        with patch(
            "hermes_codex_router.project_editing._after_registry_write",
            side_effect=SimulatedCrash(),
        ):
            with self.assertRaises(SimulatedCrash):
                store.apply(workflow_id)
        self.assertEqual(
            self.state.get_topic(topic.topic_id).execution_scope, f"root:{self.old_root}"
        )
        store.recover_pending()
        self.assertEqual(self.state.get_topic(topic.topic_id).execution_scope, f"root:{target}")

    def test_display_name_edit_preserves_identity_root_topic_and_group_binding(self) -> None:
        self._dynamic_binding()
        store, workflow_id = self._rename_ready()
        preview = store.confirmation_text(store.get(workflow_id))
        self.assertIn("Telegram", preview)
        self.assertIn("не измен", preview)

        first = store.confirm(42, workflow_id)
        second = store.confirm(42, workflow_id)
        self.assertEqual(first.workflow_id, second.workflow_id)
        completed = store.apply(workflow_id)
        repeated = store.apply(workflow_id)

        project = load_registry(self.registry_path).require_project("example")
        self.assertEqual(completed.stage, "completed")
        self.assertEqual(repeated, completed)
        self.assertEqual(project.display_name, "Renamed Project")
        self.assertEqual(project.topic_name, "Telegram Group Title")
        self.assertEqual(project.root, self.old_root)
        binding = self.state._connection.execute(
            "SELECT * FROM project_group_bindings WHERE project_id='example'"
        ).fetchone()
        assert binding is not None
        self.assertEqual(int(binding["telegram_chat_id"]), -1001234567890)
        self.assertEqual(Path(str(binding["canonical_root"])), self.old_root)

    def test_relocation_to_existing_git_root_updates_resolution_and_preserves_old_files(
        self,
    ) -> None:
        self._dynamic_binding()
        target = self.allowed_b / "prepared"
        target.mkdir()
        subprocess.run(("git", "init", "-q", str(target)), check=True)
        store, workflow_id = self._relocation_ready(target)
        self.assertIn("не перенос", store.confirmation_text(store.get(workflow_id)))
        store.confirm(42, workflow_id)
        store.apply(workflow_id)

        project = load_registry(self.registry_path).require_project("example")
        self.assertEqual(project.root, target)
        self.assertEqual((self.old_root / "keep.txt").read_text(encoding="utf-8"), "preserve")
        resolved = resolve_project_context(self.config, self.state, chat_id=-1001234567890)
        self.assertEqual(resolved.project.root, target)

    def test_relocation_can_create_project_id_child_under_another_allowed_root(self) -> None:
        self._dynamic_binding()
        target = self.allowed_b / "example"
        self.assertFalse(target.exists())
        store, workflow_id = self._relocation_ready(target)
        store.confirm(42, workflow_id)
        store.apply(workflow_id)
        self.assertTrue((target / ".git").exists())
        self.assertEqual(load_registry(self.registry_path).require_project("example").root, target)

    def test_static_binding_relocation_does_not_create_dynamic_binding(self) -> None:
        target = self.allowed_b / "prepared"
        target.mkdir()
        subprocess.run(("git", "init", "-q", str(target)), check=True)
        store, workflow_id = self._relocation_ready(target)
        store.confirm(42, workflow_id)
        store.apply(workflow_id)

        self.assertEqual(load_registry(self.registry_path).require_project("example").root, target)
        count = self.state._connection.execute(
            "SELECT COUNT(*) FROM project_group_bindings WHERE project_id='example'"
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_relocation_rolls_back_if_dynamic_binding_changed_after_selection(self) -> None:
        self._dynamic_binding()
        target = self.allowed_b / "prepared"
        target.mkdir()
        subprocess.run(("git", "init", "-q", str(target)), check=True)
        conflicting = self.allowed_b / "conflicting"
        conflicting.mkdir()
        subprocess.run(("git", "init", "-q", str(conflicting)), check=True)
        store, workflow_id = self._relocation_ready(target)
        self.state._connection.execute(
            "UPDATE project_group_bindings SET canonical_root=? WHERE project_id='example'",
            (str(conflicting),),
        )
        self.state._connection.commit()

        store.confirm(42, workflow_id)
        with self.assertRaisesRegex(Exception, "project_edit_binding_changed"):
            store.apply(workflow_id)

        self.assertEqual(
            load_registry(self.registry_path).require_project("example").root, self.old_root
        )
        binding = self.state._connection.execute(
            "SELECT canonical_root FROM project_group_bindings WHERE project_id='example'"
        ).fetchone()
        assert binding is not None
        self.assertEqual(Path(str(binding["canonical_root"])), conflicting)
        self.assertEqual(store.get(workflow_id).stage, "confirming")

    def test_root_options_are_opaque_and_free_text_cannot_select_a_path(self) -> None:
        store, workflow_id = self._start()
        store.choose_relocation(42, workflow_id)
        markup = store.root_markup(workflow_id)
        keyboard = cast(list[list[dict[str, str]]], markup["inline_keyboard"])
        callbacks = [button["callback_data"] for row in keyboard for button in row]
        self.assertTrue(callbacks)
        self.assertTrue(all(str(self.base) not in value for value in callbacks))
        with self.assertRaisesRegex(Exception, "stale"):
            store.select_root(42, str(self.allowed_b / "example"))

    def test_root_options_have_one_global_bound_across_allowed_roots(self) -> None:
        allowed_roots = [self.allowed_a, self.allowed_b]
        for index in range(MAX_ROOT_OPTIONS + 6):
            root = self.base / f"allowed-{index:02d}"
            root.mkdir()
            allowed_roots.append(root)
        document = json.loads(self.registry_path.read_text(encoding="utf-8"))
        document["allowed_roots"] = [str(root) for root in allowed_roots]
        self.registry_path.write_text(json.dumps(document), encoding="utf-8")

        store, workflow_id = self._start()
        store.choose_relocation(42, workflow_id)

        self.assertEqual(len(store.root_options(workflow_id)), MAX_ROOT_OPTIONS)

    def test_relocation_validation_rejects_unsafe_or_occupied_roots(self) -> None:
        registry = load_registry(self.registry_path)
        outside = self.base / "outside"
        outside.mkdir()
        subprocess.run(("git", "init", "-q", str(outside)), check=True)
        escape = self.allowed_b / "escape"
        escape.symlink_to(outside, target_is_directory=True)
        non_git = self.allowed_b / "non-git"
        non_git.mkdir()
        (non_git / "data.txt").write_text("keep", encoding="utf-8")
        nested = self.allowed_b / "nested" / "example"
        nested.parent.mkdir()
        duplicate = self.old_root
        other_root = self.allowed_b / "other-project"
        other_root.mkdir()
        subprocess.run(("git", "init", "-q", str(other_root)), check=True)
        registry_with_duplicate = ProjectRegistry(
            registry.schema_version,
            registry.allowed_roots,
            registry.projects + (Project("other", "Other", "Other", other_root),),
        )
        for target, create in (
            (outside, False),
            (escape, False),
            (non_git, False),
            (nested, True),
            (duplicate, False),
        ):
            with self.subTest(target=target), self.assertRaises(RegistryError):
                validate_relocation_target(
                    registry,
                    project_id="example",
                    current_root=self.old_root,
                    target=target,
                    allow_create=create,
                )
        with self.assertRaisesRegex(RegistryError, "already registered"):
            validate_relocation_target(
                registry_with_duplicate,
                project_id="example",
                current_root=self.old_root,
                target=other_root,
                allow_create=False,
            )

    def test_project_selection_is_owner_scoped(self) -> None:
        store = ProjectEditStore(self.state, self.registry_path)
        workflow = store.start(owner_user_id=42, project_ids=("example",))
        option = store.project_options(workflow.workflow_id)[0]
        with self.assertRaisesRegex(Exception, "stale"):
            store.select_project(43, option.option_id)

    def test_project_options_disambiguate_duplicate_display_names_with_project_id(self) -> None:
        other_root = self.allowed_b / "other"
        other_root.mkdir()
        subprocess.run(("git", "init", "-q", str(other_root)), check=True)
        document = json.loads(self.registry_path.read_text(encoding="utf-8"))
        document["projects"].append(
            {
                "project_id": "other",
                "display_name": "Example Project",
                "topic_name": "Other Telegram Title",
                "root": str(other_root),
                "sandbox": "workspace-write",
                "approval_policy": "on-request",
                "enabled": True,
            }
        )
        self.registry_path.write_text(json.dumps(document), encoding="utf-8")
        store = ProjectEditStore(self.state, self.registry_path)

        workflow = store.start(owner_user_id=42, project_ids=("example", "other"))
        labels = [option.safe_label for option in store.project_options(workflow.workflow_id)]

        self.assertEqual(len(labels), 2)
        self.assertEqual(len(set(labels)), 2)
        self.assertTrue(any("[example]" in label for label in labels))
        self.assertTrue(any("[other]" in label for label in labels))

    def test_legacy_long_display_name_can_be_selected_and_renamed(self) -> None:
        document = json.loads(self.registry_path.read_text(encoding="utf-8"))
        document["projects"][0]["display_name"] = "L" * 256
        self.registry_path.write_text(json.dumps(document), encoding="utf-8")

        store, workflow_id = self._start()
        self.assertEqual(store.get(workflow_id).old_display_name, "L" * 256)
        store.choose_rename(42, workflow_id)
        store.set_name(42, workflow_id, "Short local name")
        store.confirm(42, workflow_id)
        store.apply(workflow_id)

        self.assertEqual(
            load_registry(self.registry_path).require_project("example").display_name,
            "Short local name",
        )

    def test_root_option_label_strips_control_and_bidi_characters(self) -> None:
        candidate = self.allowed_b / "misleading\u202eroot\nname"
        candidate.mkdir()
        subprocess.run(("git", "init", "-q", str(candidate)), check=True)
        visually_same = self.allowed_b / "misleadingrootname"
        visually_same.mkdir()
        subprocess.run(("git", "init", "-q", str(visually_same)), check=True)
        store, workflow_id = self._start()
        store.choose_relocation(42, workflow_id)

        option = next(item for item in store.root_options(workflow_id) if item.root == candidate)
        other = next(item for item in store.root_options(workflow_id) if item.root == visually_same)
        self.assertNotIn("\u202e", option.safe_label)
        self.assertNotIn("\n", option.safe_label)
        self.assertTrue(option.safe_label.isprintable())
        self.assertNotEqual(option.safe_label, other.safe_label)
        workflow = store.select_root(42, option.option_id)
        preview = store.confirmation_text(workflow)
        self.assertNotIn("\u202e", preview)
        self.assertNotIn("\nname", preview)

    def _topic(self) -> int:
        return self.state.observe_topic(
            project_id="example",
            chat_id=-1001234567890,
            thread_id=7,
            title="Example",
        ).topic_id

    def test_relocation_blocks_attached_session_and_active_writer(self) -> None:
        self._dynamic_binding()
        target = self.allowed_b / "prepared"
        target.mkdir()
        subprocess.run(("git", "init", "-q", str(target)), check=True)
        topic_id = self._topic()
        session = self.state.activate_agent(topic_id, "codex", "model", "high")
        self.state.bind_provider_session(session.session_id, "thread-example", None)
        store, workflow_id = self._relocation_ready(target)
        store.confirm(42, workflow_id)
        with self.assertRaisesRegex(Exception, "attached_provider_session"):
            store.apply(workflow_id)
        self.state._connection.execute(
            "UPDATE agent_sessions SET status='archived' WHERE session_id=?",
            (session.session_id,),
        )
        self.state._connection.commit()
        local = self.state.activate_agent(topic_id, "codex", "model", "high")
        self.state.set_writer_mode(local.session_id, "local")
        store.confirm(42, workflow_id)
        with self.assertRaisesRegex(Exception, "active_writer"):
            store.apply(workflow_id)

    def test_relocation_blocks_queued_running_delivery_and_unresolved_work(self) -> None:
        # Each query class is covered independently so one blocker cannot mask another.
        scenarios = ("dispatch", "job", "delivery", "unresolved")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.state.close()
                self.config.state_path.unlink(missing_ok=True)
                self.state = HubState.open(self.config.state_path)
                topic_id = self._topic()
                session = self.state.activate_agent(topic_id, "codex", "model", "high")
                now = "2026-01-01T00:00:00+00:00"
                if scenario == "dispatch":
                    self.state._connection.execute(
                        """INSERT INTO turn_dispatches
                           (dispatch_id,chat_id,message_id,topic_id,agent_id,status,created_at,updated_at)
                           VALUES ('dispatch',-1001234567890,10,?,'codex','queued',?,?)""",
                        (topic_id, now, now),
                    )
                else:
                    job, _ = self.state.enqueue_provider_job(
                        idempotency_key=f"telegram:-1001234567890:{scenarios.index(scenario) + 20}",
                        chat_id=-1001234567890,
                        message_id=scenarios.index(scenario) + 20,
                        topic_id=topic_id,
                        agent_id="codex",
                        session_id=session.session_id,
                        session_generation=session.generation,
                        provider_session_id=None,
                        model="model",
                        effort="high",
                        payload_text="fictional task",
                        context_watermark=None,
                        handoff_id=None,
                    )
                    if scenario in {"delivery", "unresolved"}:
                        self.state._connection.execute(
                            "UPDATE provider_jobs SET status=? WHERE job_id=?",
                            (
                                "result_ready" if scenario == "delivery" else "indeterminate",
                                job.job_id,
                            ),
                        )
                    if scenario == "delivery":
                        self.state._connection.execute(
                            """INSERT INTO telegram_outbox
                               (outbox_id,job_id,sender_agent_id,chat_id,thread_id,telegram_html,
                                status,attempt_count,available_at,created_at,updated_at)
                               VALUES ('outbox',?,'codex',-1001234567890,7,'result','pending',0,?,?,?)""",
                            (job.job_id, now, now, now),
                        )
                self.state._connection.commit()
                store, workflow_id = self._rename_ready(f"Renamed {scenario}")
                # Rename remains legal while work exists.
                store.confirm(42, workflow_id)
                store.apply(workflow_id)
                target = self.allowed_b / f"prepared-{scenario}"
                target.mkdir()
                subprocess.run(("git", "init", "-q", str(target)), check=True)
                relocation, relocation_id = self._relocation_ready(target)
                relocation.confirm(42, relocation_id)
                expected = {
                    "dispatch": "queued_or_running_work",
                    "job": "queued_or_running_work",
                    "delivery": "pending_delivery",
                    "unresolved": "unresolved_outcome",
                }[scenario]
                with self.assertRaisesRegex(Exception, expected):
                    relocation.apply(relocation_id)

    def test_failed_progress_delivery_is_terminal_and_does_not_block_relocation(self) -> None:
        topic_id = self._topic()
        session = self.state.activate_agent(topic_id, "codex", "model", "high")
        job, _ = self.state.enqueue_provider_job(
            idempotency_key="telegram:-1001234567890:90",
            chat_id=-1001234567890,
            message_id=90,
            topic_id=topic_id,
            agent_id="codex",
            session_id=session.session_id,
            session_generation=session.generation,
            provider_session_id=None,
            model="model",
            effort="high",
            payload_text="fictional task",
            context_watermark=None,
            handoff_id=None,
        )
        now = "2026-01-01T00:00:00+00:00"
        self.state._connection.execute(
            "UPDATE provider_jobs SET status='failed' WHERE job_id=?", (job.job_id,)
        )
        self.state._connection.execute(
            """INSERT INTO provider_execution_checkpoints
               (job_id,provider_thread_id,project_root,updated_at)
               VALUES (?,'thread-terminal',?,?)""",
            (job.job_id, str(self.old_root), now),
        )
        cursor = self.state._connection.execute(
            """INSERT INTO provider_visible_items
               (job_id,item_id,phase,visible_text,created_at)
               VALUES (?,'item-terminal','commentary','terminal progress',?)""",
            (job.job_id, now),
        )
        self.state._connection.execute(
            """INSERT INTO provider_progress_deliveries
               (progress_id,item_sequence,job_id,sender_agent_id,chat_id,thread_id,
                telegram_html,status,attempt_count,available_at,created_at,updated_at)
               VALUES ('progress-terminal',?,?,'codex',-1001234567890,7,
                       'terminal progress','failed',1,?,?,?)""",
            (cursor.lastrowid, job.job_id, now, now, now),
        )
        self.state._connection.commit()
        target = self.allowed_b / "prepared"
        target.mkdir()
        subprocess.run(("git", "init", "-q", str(target)), check=True)

        store, workflow_id = self._relocation_ready(target)
        store.confirm(42, workflow_id)
        store.apply(workflow_id)

        self.assertEqual(load_registry(self.registry_path).require_project("example").root, target)

    def test_crash_after_registry_commit_is_recovered_without_second_root_or_rebind(self) -> None:
        self._dynamic_binding()
        target = self.allowed_b / "example"
        store, workflow_id = self._relocation_ready(target)
        store.confirm(42, workflow_id)
        with (
            patch(
                "hermes_codex_router.project_editing._after_registry_write",
                side_effect=SimulatedCrash(),
            ),
            self.assertRaises(SimulatedCrash),
        ):
            store.apply(workflow_id)

        self.assertEqual(load_registry(self.registry_path).require_project("example").root, target)
        self.assertEqual(store.get(workflow_id).stage, "applying")
        reopened = HubState.open(self.config.state_path)
        try:
            recovered = ProjectEditStore(reopened, self.registry_path).recover_pending()
            self.assertEqual([item.workflow_id for item in recovered], [workflow_id])
            binding = reopened._connection.execute(
                "SELECT * FROM project_group_bindings WHERE project_id='example'"
            ).fetchone()
            assert binding is not None
            self.assertEqual(Path(str(binding["canonical_root"])), target)
            self.assertEqual(
                reopened._connection.execute(
                    "SELECT COUNT(*) FROM project_group_bindings WHERE project_id='example'"
                ).fetchone()[0],
                1,
            )
        finally:
            reopened.close()

    def test_recovery_binding_conflict_remains_a_startup_barrier(self) -> None:
        self._dynamic_binding()
        target = self.allowed_b / "example"
        store, workflow_id = self._relocation_ready(target)
        store.confirm(42, workflow_id)
        with (
            patch(
                "hermes_codex_router.project_editing._after_registry_write",
                side_effect=SimulatedCrash(),
            ),
            self.assertRaises(SimulatedCrash),
        ):
            store.apply(workflow_id)
        conflicting = self.allowed_b / "missing-binding-root"
        self.state._connection.execute(
            "UPDATE project_group_bindings SET canonical_root=? WHERE project_id='example'",
            (str(conflicting),),
        )
        self.state._connection.commit()

        for _attempt in range(2):
            with self.assertRaises(FileNotFoundError):
                store.recover_pending()
            self.assertEqual(store.get(workflow_id).stage, "applying")

        self.assertEqual(load_registry(self.registry_path).require_project("example").root, target)

    def test_crash_before_registry_commit_recovers_from_durable_intent(self) -> None:
        self._dynamic_binding()
        target = self.allowed_b / "example"
        store, workflow_id = self._relocation_ready(target)
        store.confirm(42, workflow_id)
        with (
            patch(
                "hermes_codex_router.project_editing._atomic_write",
                side_effect=SimulatedCrash(),
            ),
            self.assertRaises(SimulatedCrash),
        ):
            store.apply(workflow_id)

        self.assertEqual(
            load_registry(self.registry_path).require_project("example").root, self.old_root
        )
        self.assertEqual(store.get(workflow_id).stage, "applying")
        recovered = store.recover_pending()
        self.assertEqual([item.workflow_id for item in recovered], [workflow_id])
        self.assertEqual(load_registry(self.registry_path).require_project("example").root, target)

    def test_ordinary_commit_fault_rolls_back_registry_and_binding(self) -> None:
        self._dynamic_binding()
        target = self.allowed_b / "prepared"
        target.mkdir()
        subprocess.run(("git", "init", "-q", str(target)), check=True)
        store, workflow_id = self._relocation_ready(target)
        store.confirm(42, workflow_id)
        with (
            patch(
                "hermes_codex_router.project_editing._after_registry_write",
                side_effect=RuntimeError("fictional commit fault"),
            ),
            self.assertRaisesRegex(RuntimeError, "fictional commit fault"),
        ):
            store.apply(workflow_id)

        self.assertEqual(
            load_registry(self.registry_path).require_project("example").root, self.old_root
        )
        binding = self.state._connection.execute(
            "SELECT canonical_root FROM project_group_bindings WHERE project_id='example'"
        ).fetchone()
        assert binding is not None
        self.assertEqual(Path(str(binding["canonical_root"])), self.old_root)
        self.assertEqual(store.get(workflow_id).stage, "confirming")

    def test_archived_provider_origin_is_never_rebound_to_new_root(self) -> None:
        self._dynamic_binding()
        topic_id = self._topic()
        session = self.state.activate_agent(topic_id, "codex", "model", "high")
        self.state.bind_provider_session(session.session_id, "thread-example", None)
        self.state._connection.execute(
            """INSERT INTO codex_session_origins
               (session_id,provider_thread_id,project_id,canonical_root,model_provider,created_at)
               VALUES (?, 'thread-example','example',?,'openai','2026-01-01T00:00:00+00:00')""",
            (session.session_id, str(self.old_root)),
        )
        self.state._connection.execute(
            "UPDATE agent_sessions SET status='archived' WHERE session_id=?",
            (session.session_id,),
        )
        self.state._connection.commit()
        target = self.allowed_b / "prepared"
        target.mkdir()
        subprocess.run(("git", "init", "-q", str(target)), check=True)
        store, workflow_id = self._relocation_ready(target)
        store.confirm(42, workflow_id)
        store.apply(workflow_id)
        origin = self.state._connection.execute(
            "SELECT canonical_root FROM codex_session_origins WHERE session_id=?",
            (session.session_id,),
        ).fetchone()
        assert origin is not None
        self.assertEqual(Path(str(origin["canonical_root"])), self.old_root)


if __name__ == "__main__":
    unittest.main()
