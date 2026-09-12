from __future__ import annotations

import io
import json
import sqlite3
import stat
import subprocess
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import test_codex_worker as worker_fixtures

from hermes_codex_router.codex_appserver import CodexThreadMetadata, RpcError
from hermes_codex_router.codex_session_adoption import (
    AdoptionError,
    attach_codex_session,
    inspect_codex_session,
)
from hermes_codex_router.state import HubState


class CodexSessionAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture = worker_fixtures.CodexQueueWorkerTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.config = replace(fixture.config, outbox_runtime="external")
        self.root = fixture.registry.projects[0].root
        subprocess.run(("git", "init", "-q", str(self.root)), check=True)
        self.config.registry_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "allowed_roots": [str(self.root.parent)],
                    "projects": [
                        {
                            "project_id": "example-project",
                            "display_name": "Example",
                            "topic_name": "Example",
                            "root": str(self.root),
                        }
                    ],
                }
            )
        )
        state = HubState.open(self.config.state_path)
        try:
            state.observe_topic(
                project_id="example-project", chat_id=-1001234567890, thread_id=7, title="Example"
            )
        finally:
            state.close()
        self.inspector = Mock(
            return_value=CodexThreadMetadata("example-thread", self.root, "openai", "notLoaded")
        )

    def run_attach(self, **kwargs):
        return attach_codex_session(
            self.config,
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=7,
            codex_thread_id="example-thread",
            inspector=self.inspector,
            **kwargs,
        )

    def test_preview_reads_only_and_apply_requires_explicit_confirmation(self) -> None:
        before = self.config.state_path.read_bytes()
        result = self.run_attach()
        self.assertEqual(result["action"], "preview")
        self.assertEqual(self.config.state_path.read_bytes(), before)
        with self.assertRaisesRegex(AdoptionError, "cli_close_confirmation_required"):
            self.run_attach(apply=True)
        self.assertEqual(self.config.state_path.read_bytes(), before)
        result = self.run_attach(apply=True, confirm_cli_closed=True)
        self.assertEqual(result["writer_mode"], "local")
        self.assertEqual(result["action"], "attached")

    def test_exact_repeat_needs_no_provider_and_preserves_returned_writer(self) -> None:
        result = self.run_attach(apply=True, confirm_cli_closed=True)
        state = HubState.open(self.config.state_path)
        try:
            topic = state.find_topic(-1001234567890, 7)
            assert topic is not None
            session_id = result["hub_session_id"]
            assert isinstance(session_id, str)
            state.return_codex_local_writer(
                chat_id=topic.chat_id,
                message_id=30,
                topic_id=topic.topic_id,
                session_id=session_id,
                observer_agent_id="hub",
            )
        finally:
            state.close()
        self.inspector.side_effect = AssertionError("idempotency must not use provider")
        repeated = self.run_attach(apply=True, confirm_cli_closed=True)
        self.assertEqual(repeated["action"], "already_attached")
        self.assertEqual(repeated["writer_mode"], "telegram")

    def test_job_admitted_during_metadata_read_wins_without_partial_attachment(self) -> None:
        state = HubState.open(self.config.state_path)
        try:
            topic = state.find_topic(-1001234567890, 7)
            assert topic is not None
            old = state.activate_agent(topic.topic_id, "codex", "gpt-5.6-sol", "high")

            def admit(*args):
                state.enqueue_provider_job(
                    idempotency_key="concurrent-admission",
                    chat_id=topic.chat_id,
                    message_id=1,
                    topic_id=topic.topic_id,
                    agent_id="codex",
                    session_id=old.session_id,
                    session_generation=old.generation,
                    provider_session_id=None,
                    model=old.model,
                    effort=old.effort,
                    payload_text="Concurrent request",
                )
                return CodexThreadMetadata("example-thread", self.root, "openai", "notLoaded")

            self.inspector.side_effect = admit
            with self.assertRaisesRegex(AdoptionError, "busy"):
                self.run_attach(apply=True, confirm_cli_closed=True)
            self.assertEqual(state.active_session(topic.topic_id), old)
            self.assertEqual(
                state._connection.execute("SELECT COUNT(*) FROM codex_session_origins").fetchone()[
                    0
                ],
                0,
            )
        finally:
            state.close()

    def test_missing_or_old_database_never_creates_or_migrates(self) -> None:
        self.config = replace(self.config, state_path=self.root.parent / "missing.db")
        with self.assertRaisesRegex(AdoptionError, "state_unavailable"):
            self.run_attach()
        self.assertFalse(self.config.state_path.exists())
        connection = sqlite3.connect(self.config.state_path)
        connection.execute("PRAGMA user_version=24")
        connection.close()
        self.config.state_path.chmod(0o600)
        self.assertEqual(stat.S_IMODE(self.config.state_path.stat().st_mode), 0o600)
        before = self.config.state_path.read_bytes()
        with self.assertRaisesRegex(AdoptionError, "schema_upgrade_required"):
            self.run_attach()
        self.assertEqual(self.config.state_path.read_bytes(), before)
        self.inspector.assert_not_called()

    def test_invalid_input_mode_model_and_root_stop_before_inspection(self) -> None:
        for changes in (
            {"dispatch_mode": "inline"},
            {"queue_runtime": "embedded"},
            {"outbox_runtime": "controller"},
            {"external_worker_agent_ids": ("opencode",)},
        ):
            with self.subTest(changes=changes):
                config = self.config
                self.config = replace(config, **changes)
                with self.assertRaises(AdoptionError):
                    self.run_attach()
                self.config = config
        with self.assertRaises(AdoptionError):
            self.run_attach(model="unknown-model")
        self.inspector.assert_not_called()

    def test_topic_identity_is_bounded_and_group_mismatch_has_specific_reason(self) -> None:
        for chat_id, thread_id in ((True, 7), (-1001, False), (-(2**70), 7), (-1001, 2**70)):
            with (
                self.subTest(chat_id=chat_id, thread_id=thread_id),
                self.assertRaisesRegex(AdoptionError, "invalid_topic_identity"),
            ):
                attach_codex_session(
                    self.config,
                    project_id="example-project",
                    chat_id=chat_id,
                    thread_id=thread_id,
                    codex_thread_id="example-thread",
                    inspector=self.inspector,
                )
        with self.assertRaisesRegex(AdoptionError, "project_binding_mismatch"):
            attach_codex_session(
                self.config,
                project_id="different-project",
                chat_id=-1001234567890,
                thread_id=7,
                codex_thread_id="example-thread",
                inspector=self.inspector,
            )
        self.inspector.assert_not_called()

    def test_inspector_failure_leaves_binding_unchanged(self) -> None:
        before = self.config.state_path.read_bytes()
        self.inspector.side_effect = RpcError("secret provider payload")
        with self.assertRaises(AdoptionError) as raised:
            self.run_attach(apply=True, confirm_cli_closed=True)
        self.assertNotIn("secret", str(raised.exception))
        self.assertEqual(self.config.state_path.read_bytes(), before)

    def test_inspector_closes_transport_on_initialize_or_read_failure(self) -> None:
        self.config = replace(self.config, codex_stdio_executable=Path("/bin/false"))
        for phase in ("initialize", "read_thread_metadata"):
            with self.subTest(phase=phase):
                transport = Mock()
                client = Mock()
                getattr(client, phase).side_effect = RpcError("private detail")
                with (
                    patch(
                        "hermes_codex_router.codex_session_adoption.StdioJsonLineTransport.start",
                        return_value=transport,
                    ),
                    patch(
                        "hermes_codex_router.codex_session_adoption.CodexAppServerClient",
                        return_value=client,
                    ),
                ):
                    with self.assertRaises(RpcError):
                        inspect_codex_session(self.config, "example-thread", self.root)
                    client.close.assert_called_once()
                client.start_thread.assert_not_called()
                client.resume_thread.assert_not_called()
                client.start_turn.assert_not_called()

    def test_cli_uses_real_config_loader_without_token_reads_or_productive_rpc(self) -> None:
        from hermes_codex_router.cli import main

        token_path = self.root.parent / "must-not-read.token"
        config_path = self.root.parent / "hub.json"
        config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "owner_user_ids": [42],
                    "registry_path": str(self.config.registry_path),
                    "state_path": str(self.config.state_path),
                    "codex_socket_path": str(self.config.codex_socket_path),
                    "codex_stdio_executable": "/bin/false",
                    "manage_codex_server": False,
                    "dispatch_mode": "queue",
                    "queue_runtime": "external",
                    "outbox_runtime": "external",
                    "external_worker_agent_ids": ["codex"],
                    "projects": [
                        {"project_id": "example-project", "telegram_chat_id": -1001234567890}
                    ],
                    "agents": [
                        {
                            "agent_id": "codex",
                            "display_name": "Codex",
                            "telegram_username": "example_codex_bot",
                            "runtime": "codex",
                            "token_file": str(token_path),
                            "default_model": "gpt-5.6-sol",
                            "default_effort": "high",
                        }
                    ],
                }
            )
        )
        args = [
            "session",
            "attach-codex",
            str(config_path),
            "--project",
            "example-project",
            "--chat-id=-1001234567890",
            "--thread-id=7",
            "--codex-thread-id=example-thread",
            "--json",
        ]
        before = self.config.state_path.read_bytes()
        client = Mock()
        client.read_thread_metadata.return_value = self.inspector.return_value
        with (
            patch("hermes_codex_router.codex_session_adoption.StdioJsonLineTransport.start"),
            patch(
                "hermes_codex_router.codex_session_adoption.CodexAppServerClient",
                return_value=client,
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertEqual(main(args), 0)
        self.assertEqual(json.loads(output.getvalue())["action"], "preview")
        self.assertEqual(before, self.config.state_path.read_bytes())
        self.assertFalse(token_path.exists())
        client.start_thread.assert_not_called()
        client.resume_thread.assert_not_called()
        client.start_turn.assert_not_called()
        client.close.assert_called_once()
