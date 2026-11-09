from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.claude_permissions_config import parse_claude_file_permissions
from hermes_codex_router.external_runtime import ExternalCliAdapter, ProviderUnavailableError


class ClaudeHostProfileTests(unittest.TestCase):
    def test_minimal_explicit_host_config(self):
        config = parse_claude_file_permissions(
            {"profile": "host", "provider_home": "/home/example/sessions"}
        )
        assert config is not None
        self.assertEqual(config.profile, "host")

    def test_direct_argv_environment_and_stream_policy(self):
        from hermes_codex_router.claude_host_profile import ClaudeHostConfig

        root = Path.cwd()
        host = ClaudeHostConfig(root, root / "session", Path("/usr/bin/true"))
        adapter = ExternalCliAdapter("claude")
        env = {
            "HOME": "/home/example",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
            "ANTHROPIC_AUTH_TOKEN": "example",
            "AWS_SECRET_ACCESS_KEY": "example",
            "GOOGLE_APPLICATION_CREDENTIALS": "/home/example/key",
            "CLAUDE_CODE_OAUTH_TOKEN": "",
            "CLAUDE_CODE_USE_BEDROCK": "",
            "ANTHROPIC_API_KEY": "",
        }
        output = '{"type":"result","subtype":"success","is_error":false,"session_id":"00000000-0000-4000-8000-000000000001","result":"ok"}\n'
        with (
            patch.dict(os.environ, env, clear=True),
            patch(
                "hermes_codex_router.external_runtime.pwd.getpwuid",
                return_value=__import__("types").SimpleNamespace(pw_dir="/home/example"),
            ),
            patch("hermes_codex_router.external_runtime.validate_host_route_settings"),
            patch.object(adapter, "_verified_claude_argv", side_effect=lambda argv, **kwargs: argv),
            patch.object(
                adapter,
                "_run_claude_process",
                return_value=subprocess.CompletedProcess([], 0, output, ""),
            ) as run,
        ):
            adapter.run_turn(cwd=root, prompt="hello", claude_sandbox=host)
        args, kwargs = run.call_args
        argv = args[0]
        self.assertEqual(argv[0], "/usr/bin/true")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")
        for forbidden in (
            "--tools",
            "--restricted",
            "--safe-mode",
            "--setting-sources",
            "--strict-mcp-config",
            "--settings",
        ):
            self.assertNotIn(forbidden, argv)
        self.assertEqual(kwargs["cwd"], root)
        self.assertEqual(kwargs["environment"]["HOME"], "/home/example")
        self.assertEqual(kwargs["environment"]["CLAUDE_CONFIG_DIR"], str(root / "session"))
        self.assertNotIn("HUB_CLAUDE_PERMISSION_SOCKET", kwargs["environment"])
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", kwargs["environment"])
        self.assertNotIn("GOOGLE_APPLICATION_CREDENTIALS", kwargs["environment"])
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", kwargs["environment"])
        self.assertNotIn("CLAUDE_CODE_USE_BEDROCK", kwargs["environment"])
        self.assertNotIn("ANTHROPIC_API_KEY", kwargs["environment"])
        kwargs["event_policy"]({"type": "tool_progress"})

    def test_host_refuses_subscription_route(self):
        from hermes_codex_router.claude_host_profile import ClaudeHostConfig

        adapter = ExternalCliAdapter("claude", claude_auth_route="subscription")
        host = ClaudeHostConfig(Path.cwd(), Path.cwd() / "session", Path("/usr/bin/true"))
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "example"}, clear=True):
            with self.assertRaises(ProviderUnavailableError):
                adapter.run_turn(cwd=Path.cwd(), prompt="hello", claude_sandbox=host)


class ClaudeHostRouteTests(unittest.TestCase):
    def test_route_settings_reject_credential_overrides_but_allow_customizations(self):
        from hermes_codex_router.claude_host_profile import validate_host_route_settings

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            home = Path(temporary) / "session"
            home.mkdir()
            settings = root / ".claude"
            settings.mkdir()
            path = settings / "settings.json"
            path.write_text(json.dumps({"env": {"EXAMPLE": "yes"}, "hooks": {}}))
            validate_host_route_settings(root, home)
            for data in (
                {"apiKeyHelper": "bad"},
                {"env": {"ANTHROPIC_BASE_URL": "bad"}},
                {"env": {"CLAUDE_CODE_USE_BEDROCK": "1"}},
                {"env": {"CLAUDE_CONFIG_DIR": "/home/example/.claude"}},
            ):
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    validate_host_route_settings(root, home)


class ClaudeHostBindingTests(unittest.TestCase):
    def test_new_host_binds_and_never_opens_permission_transport(self):
        from dataclasses import replace

        from hermes_codex_router import claude_permission_host as module
        from hermes_codex_router.execution_journal import ExecutionJournal
        from tests.test_claude_session_mode_preflight import ClaudeSessionModePreflightTests

        fixture = ClaudeSessionModePreflightTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config = fixture.config()
        assert config.claude_file_permissions is not None
        config = replace(
            config, claude_file_permissions=replace(config.claude_file_permissions, profile="host")
        )
        state = fixture.fixture.state
        native = fixture.fixture.launch.session_id
        with patch.object(module, "load_tlive_permission_config") as transport:
            with self.assertRaises(ProviderUnavailableError) as error:
                with module.hosted_claude_launch(
                    config,
                    state,
                    config.agents[0],
                    fixture.fixture.job,
                    fixture.fixture.token,
                    native,
                    fixture.fixture.root,
                ):
                    self.fail("old session resumed")
            self.assertEqual(error.exception.code, "claude_session_mode_changed")
            transport.assert_not_called()
        self.assertEqual(fixture.check_modes(profile="host"), (1, 1))
        fixture.fixture.journal.close_launch(fixture.fixture.launch)
        with state._connection:
            state._connection.execute(
                "UPDATE provider_jobs SET status='completed', lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL WHERE job_id=?",
                (fixture.fixture.job.job_id,),
            )
        fresh = state.new_active_session(
            fixture.fixture.job.topic_id, expected_session_id=fixture.fixture.session.session_id
        )
        job, _ = state.enqueue_provider_job(
            idempotency_key="example:host-new",
            chat_id=fixture.fixture.job.chat_id,
            message_id=2,
            topic_id=fixture.fixture.job.topic_id,
            agent_id="claude",
            session_id=fresh.session_id,
            session_generation=fresh.generation,
            model="example-model",
            effort="high",
            payload_text="Example",
        )
        leased = state.lease_provider_job("claude", "example-worker")
        assert leased is not None and leased.lease_token is not None
        token = leased.lease_token
        state.mark_provider_job_executing(job.job_id, token)
        binding = ExecutionJournal(state).prepare_claude_session(
            job.job_id, token, fixture.fixture.root
        )
        self.assertTrue(binding.is_new)
        self.assertNotEqual(binding.session_id, native)
        native = binding.session_id
        fixture.home.mkdir(mode=0o700)
        (fixture.home / native).mkdir(mode=0o700)
        (fixture.home / native).chmod(0o500)
        with self.assertRaises(ProviderUnavailableError) as unsafe:
            with module.hosted_claude_launch(
                config,
                state,
                config.agents[0],
                job,
                token,
                native,
                fixture.fixture.root,
                is_new=True,
            ):
                self.fail("read-only session storage accepted")
        self.assertEqual(unsafe.exception.code, "claude_host_unverified")
        (fixture.home / native).chmod(0o700)
        with (
            patch.object(module, "load_tlive_permission_config") as transport,
            patch.object(module, "PermissionServer") as socket_server,
            patch.object(module, "FileToolSandboxConfig") as confined,
        ):
            with module.hosted_claude_launch(
                config,
                state,
                config.agents[0],
                job,
                token,
                native,
                fixture.fixture.root,
                is_new=True,
            ) as hosted:
                assert hosted is not None
                self.assertEqual(hosted.sandbox.profile, "host")
                self.assertEqual(hosted.sandbox.provider_home, fixture.home / native)
            transport.assert_not_called()
            socket_server.assert_not_called()
            confined.assert_not_called()
        self.assertEqual(fixture.check_modes(profile="host"), (1, 0))
        self.assertEqual(
            state._connection.execute(
                "SELECT status FROM claude_permission_launches WHERE job_id=?", (job.job_id,)
            ).fetchone()[0],
            "closed",
        )


class ClaudeHostStopTests(unittest.TestCase):
    def test_direct_process_group_stops_without_replay(self):
        from contextlib import nullcontext
        from typing import Any, cast

        from hermes_codex_router.claude_host_profile import ClaudeHostConfig
        from hermes_codex_router.claude_permission_host import HostedClaudeLaunch
        from hermes_codex_router.execution_journal import ExecutionJournal
        from tests import test_claude_stop_precedence as fixtures
        from tests.test_claude_cli_capabilities import HELP
        from tests.test_claude_native_worker import OTHER_UUID

        controls = fixtures.ClaudeStopPrecedenceTests()
        controls.setUp()
        self.addCleanup(controls.doCleanups)
        fixture, job_id = controls.fixture()
        visible = {
            "type": "assistant",
            "uuid": OTHER_UUID,
            "parent_tool_use_id": None,
            "message": {"content": [{"type": "text", "text": "Fictional partial"}]},
        }
        body = (
            f"visible={visible!r}\nvisible['session_id']=native\n"
            "os.write(1,(json.dumps(visible)+'\\n').encode())\ntime.sleep(30)\n"
        )
        adapter = controls.adapter(fixture.root, body)
        child = Path(adapter.executable)
        host_help = HELP.replace(
            "Values: default, dontAsk, plan", "Values: default, dontAsk, plan, bypassPermissions"
        )
        child.write_text(child.read_text().replace(repr(HELP), repr(host_help)))
        worker = fixture.worker(cast(Any, adapter))
        host = ClaudeHostConfig(fixture.root, fixture.root / "session", child)
        original = ExecutionJournal.record_claude_item
        recorded = []

        def record(journal, *args, **kwargs):
            original(journal, *args, **kwargs)
            recorded.append(True)
            controls.request_stop(worker, job_id, adapter)

        with (
            patch(
                "hermes_codex_router.claude_permission_host.hosted_claude_launch",
                return_value=nullcontext(HostedClaudeLaunch(host)),
            ),
            patch.object(ExecutionJournal, "record_claude_item", new=record),
        ):
            self.assertTrue(worker.run_cycle())
        self.assertEqual(recorded, [True])
        controls.assert_state(worker, job_id, uncertain=False)
        self.assertIsNone(adapter._active_process)


class ClaudeHostReviewTests(unittest.TestCase):
    def test_host_capability_needs_bypass_not_confined_controls(self):
        from hermes_codex_router.claude_cli_capabilities import _has_required_advertisement
        from tests.test_claude_cli_capabilities import HELP

        bypass = HELP.replace(
            "Values: default, dontAsk, plan", "Values: default, plan, bypassPermissions"
        )
        unconfined = bypass.replace("  --permission-prompts <mode>", "  --other-prompts <mode>")
        self.assertTrue(_has_required_advertisement(unconfined.encode(), host=True))
        self.assertFalse(_has_required_advertisement(unconfined.encode()))
        self.assertFalse(_has_required_advertisement(HELP.encode(), host=True))
        self.assertFalse(_has_required_advertisement(bypass.encode(), host=True, image_input=True))

    def test_host_structured_input_requires_image_capability(self):
        from hermes_codex_router.claude_host_profile import ClaudeHostConfig

        root = Path.cwd()
        host = ClaudeHostConfig(root, root / "session", Path("/usr/bin/true"))
        adapter = ExternalCliAdapter("claude")
        with (
            patch("hermes_codex_router.external_runtime.validate_host_route_settings"),
            patch.object(
                adapter, "_verified_claude_argv", side_effect=ProviderUnavailableError("x", "x")
            ) as verified,
            patch.object(adapter, "_run_claude_process") as run,
        ):
            with self.assertRaises(ProviderUnavailableError):
                adapter._run_owned_claude_turn(
                    ("claude",),
                    cwd=root,
                    environment={},
                    timeout=1,
                    expected_session_id=None,
                    on_visible_assistant=None,
                    on_process_started=None,
                    sandbox=host,
                    input_data=b"{}\n",
                    correction_input=object(),  # type: ignore[arg-type]
                )
        self.assertTrue(verified.call_args.kwargs["image_input"])
        run.assert_not_called()


class ClaudeHostScopeTests(unittest.TestCase):
    def setUp(self):
        from hermes_codex_router.claude_host_profile import ClaudeHostConfig

        self.root = Path.cwd()
        self.host = ClaudeHostConfig(self.root, self.root / "session", Path("/usr/bin/true"))
        self.adapter = ExternalCliAdapter("claude")
        self.output = '{"type":"result","subtype":"success","is_error":false,"session_id":"00000000-0000-4000-8000-000000000001","result":"ok"}\n'
        self.env = {
            "INVOCATION_ID": "example",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
            "ANTHROPIC_AUTH_TOKEN": "example",
        }
        self.stack = __import__("contextlib").ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, self.env, clear=True))
        self.stack.enter_context(
            patch(
                "hermes_codex_router.external_runtime.pwd.getpwuid",
                return_value=__import__("types").SimpleNamespace(pw_dir="/home/example"),
            )
        )
        self.stack.enter_context(
            patch("hermes_codex_router.external_runtime.validate_host_route_settings")
        )
        self.verified = self.stack.enter_context(
            patch.object(self.adapter, "_verified_claude_argv", side_effect=lambda argv, **_: argv)
        )
        self.which = self.stack.enter_context(
            patch(
                "hermes_codex_router.claude_host_profile.shutil.which",
                side_effect=lambda name: "/usr/bin/" + name,
            )
        )
        self.systemctl = self.stack.enter_context(
            patch(
                "hermes_codex_router.claude_host_profile.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, "inactive\n", ""),
            )
        )
        self.process_run = self.stack.enter_context(
            patch.object(
                self.adapter,
                "_run_claude_process",
                return_value=subprocess.CompletedProcess([], 0, self.output, ""),
            )
        )

    def turn(self):
        return self.adapter.run_turn(cwd=self.root, prompt="hello", claude_sandbox=self.host)

    def test_scope_prefix_binds_verified_claude_and_success_preserves_scope(self):
        self.turn()
        argv = self.process_run.call_args.args[0]
        self.assertEqual(argv[:4], ("/usr/bin/systemd-run", "--user", "--scope", "--quiet"))
        self.assertEqual(argv[4], "--collect")
        self.assertRegex(argv[5], r"^--unit=hub-claude-host-[0-9a-f]{12}\.scope$")
        self.assertEqual(argv[6:8], ("--", "/usr/bin/true"))
        self.assertEqual(self.verified.call_args.args[0][0], "/usr/bin/true")
        self.assertEqual(
            self.process_run.call_args.kwargs["environment"][
                "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"
            ],
            "1",
        )
        self.systemctl.assert_not_called()

    def test_outside_systemd_unchanged(self):
        os.environ.pop("INVOCATION_ID")
        self.turn()
        self.assertEqual(self.process_run.call_args.args[0][0], "/usr/bin/true")
        self.which.assert_not_called()
        self.systemctl.assert_not_called()

    def test_stop_timeout_and_failure_kill_and_poll(self):
        from hermes_codex_router.external_runtime import (
            ExternalRuntimeError,
            ExternalTurnInterrupted,
        )

        for failure in (
            ExternalTurnInterrupted("stop"),
            ExternalRuntimeError("timeout"),
            ValueError("failure"),
        ):
            with self.subTest(failure=type(failure).__name__):
                self.systemctl.reset_mock()
                self.process_run.side_effect = failure
                with self.assertRaises(type(failure)):
                    self.turn()
                argv = self.process_run.call_args.args[0]
                unit = argv[5].removeprefix("--unit=")
                calls = [call.args[0] for call in self.systemctl.call_args_list]
                self.assertEqual(
                    calls,
                    [
                        [
                            "/usr/bin/systemctl",
                            "--user",
                            "kill",
                            "--signal=SIGKILL",
                            "--kill-whom=all",
                            "--",
                            unit,
                        ],
                        [
                            "/usr/bin/systemctl",
                            "--user",
                            "show",
                            "-p",
                            "ActiveState",
                            "--value",
                            "--",
                            unit,
                        ],
                    ],
                )

    def test_terminal_failure_is_cleaned_up(self):
        self.process_run.return_value = subprocess.CompletedProcess([], 1, self.output, "")
        with self.assertRaises(Exception):
            self.turn()
        self.assertEqual(self.systemctl.call_count, 2)

    def test_pending_cleanup_preserves_failure_and_blocks_until_inactive(self):
        failure = ValueError("original")
        self.process_run.side_effect = failure
        self.systemctl.return_value = subprocess.CompletedProcess([], 0, "active\n", "")
        with patch("hermes_codex_router.claude_host_profile.CLEANUP_TIMEOUT", 0):
            with self.assertRaises(ValueError) as caught:
                self.turn()
        self.assertIs(caught.exception, failure)
        self.process_run.reset_mock()
        with self.assertRaises(ProviderUnavailableError) as pending:
            self.turn()
        self.assertEqual(pending.exception.code, "claude_host_cleanup_pending")
        self.process_run.assert_not_called()
        self.systemctl.return_value = subprocess.CompletedProcess([], 0, "inactive\n", "")
        self.process_run.side_effect = None
        self.turn()
        self.assertEqual(self.process_run.call_count, 1)

    def test_unverified_stopped_scope_does_not_claim_stop_proof(self):
        from hermes_codex_router.external_runtime import ExternalTurnInterrupted

        failure = ExternalTurnInterrupted("stop")
        self.process_run.side_effect = failure
        self.systemctl.return_value = subprocess.CompletedProcess([], 0, "active\n", "")
        with patch("hermes_codex_router.claude_host_profile.CLEANUP_TIMEOUT", 0):
            with self.assertRaises(ExternalTurnInterrupted) as caught:
                self.turn()
        self.assertIs(caught.exception, failure)

    def test_poll_errors_remain_pending_and_disappearance_clears_it(self):
        self.process_run.side_effect = ValueError("original")
        self.systemctl.side_effect = OSError("unavailable")
        with patch("hermes_codex_router.claude_host_profile.CLEANUP_TIMEOUT", 0):
            with self.assertRaises(ValueError):
                self.turn()
        self.systemctl.side_effect = None
        self.systemctl.return_value = subprocess.CompletedProcess([], 1, "", "bus unavailable")
        with self.assertRaises(ProviderUnavailableError) as pending:
            self.turn()
        self.assertEqual(pending.exception.code, "claude_host_cleanup_pending")
        self.systemctl.return_value = subprocess.CompletedProcess(
            [], 1, "", f"Unit {next(iter(self.adapter._host_scopes.pending))} could not be found."
        )
        self.process_run.side_effect = None
        self.turn()

    def test_distinct_turns_get_distinct_scopes(self):
        self.turn()
        first = self.process_run.call_args.args[0][5]
        self.turn()
        self.assertNotEqual(first, self.process_run.call_args.args[0][5])
        self.assertEqual(self.which.call_count, 2)

    def test_active_scope_is_polled_until_inactive(self):
        self.process_run.side_effect = ValueError("failure")
        self.systemctl.side_effect = [
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "active\n", ""),
            subprocess.CompletedProcess([], 0, "inactive\n", ""),
        ]
        with patch("hermes_codex_router.claude_host_profile.time.sleep"):
            with self.assertRaises(ValueError):
                self.turn()
        self.assertEqual(self.systemctl.call_count, 3)
        self.assertFalse(self.adapter._host_scopes.pending)
        for call in self.systemctl.call_args_list:
            self.assertNotIn("ANTHROPIC_AUTH_TOKEN", call.kwargs["env"])

    def test_invalid_unit_name_is_refused_before_launch(self):
        invalid = __import__("types").SimpleNamespace(hex="invalid/scope")
        with patch("hermes_codex_router.claude_host_profile.uuid.uuid4", return_value=invalid):
            with self.assertRaises(ProviderUnavailableError) as error:
                self.turn()
        self.assertEqual(error.exception.code, "claude_host_unverified")
        self.process_run.assert_not_called()
        self.systemctl.assert_not_called()

    def test_missing_systemd_tools_refuse(self):
        for missing in ("systemd-run", "systemctl"):
            with self.subTest(missing=missing):
                self.which.side_effect = lambda name: (
                    None if name == missing else "/usr/bin/" + name
                )
                with self.assertRaises(ProviderUnavailableError) as error:
                    self.turn()
                self.assertEqual(error.exception.code, "claude_host_unverified")
                self.process_run.assert_not_called()
