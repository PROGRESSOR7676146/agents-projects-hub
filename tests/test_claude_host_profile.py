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


class ClaudeHostStrayCleanupTests(unittest.TestCase):
    def cgroup(self, temporary, relative, pids):
        proc = Path(temporary) / "self-cgroup"
        proc.write_text(f"0::{relative}\n")
        directory = Path(temporary) / "root" / relative.lstrip("/")
        directory.mkdir(parents=True)
        (directory / "cgroup.procs").write_text("".join(f"{pid}\n" for pid in pids))
        return proc, Path(temporary) / "root", directory / "cgroup.procs"

    def test_kills_only_other_processes_in_the_worker_service_cgroup(self):
        import signal

        from hermes_codex_router.claude_host_profile import reap_cgroup_strays

        with tempfile.TemporaryDirectory() as temporary:
            proc, root, procs = self.cgroup(
                temporary, "/user.slice/app.slice/example-worker@2.service", (10, 11, 12)
            )
            killed = []

            def kill(pid, number):
                killed.append((pid, number))
                procs.write_text("10\n12\n" if number == signal.SIGTERM else "10\n")

            left = reap_cgroup_strays(
                proc_cgroup=proc,
                cgroup_root=root,
                environ={"INVOCATION_ID": "example"},
                own_pid=10,
                kill=kill,
                sleep=lambda _: None,
            )
        self.assertEqual(left, 0)
        self.assertNotIn(10, {pid for pid, _ in killed})
        self.assertEqual(
            sorted(killed),
            sorted([(11, signal.SIGTERM), (12, signal.SIGTERM), (12, signal.SIGKILL)]),
        )

    def test_outside_a_service_nothing_is_killed(self):
        from hermes_codex_router.claude_host_profile import reap_cgroup_strays

        with tempfile.TemporaryDirectory() as temporary:
            for relative, environ in (
                ("/user.slice/app.slice/example-worker@2.service", {}),
                ("/user.slice/user@1000.service", {"INVOCATION_ID": "example"}),
                ("/user.slice/session-1.scope", {"INVOCATION_ID": "example"}),
            ):
                with self.subTest(relative=relative):
                    sub = Path(temporary) / str(abs(hash(relative)))
                    sub.mkdir()
                    proc, root, _ = self.cgroup(str(sub), relative, (10, 11))
                    killed = []
                    reap_cgroup_strays(
                        proc_cgroup=proc,
                        cgroup_root=root,
                        environ=environ,
                        own_pid=10,
                        kill=lambda pid, number: killed.append(pid),
                        sleep=lambda _: None,
                    )
                    self.assertEqual(killed, [])

    def test_stopped_host_turn_reaps_and_successful_turn_does_not(self):
        from hermes_codex_router.claude_host_profile import ClaudeHostConfig
        from hermes_codex_router.external_runtime import ExternalTurnInterrupted

        root = Path.cwd()
        host = ClaudeHostConfig(root, root / "session", Path("/usr/bin/true"))
        adapter = ExternalCliAdapter("claude")
        output = '{"type":"result","subtype":"success","is_error":false,"session_id":"00000000-0000-4000-8000-000000000001","result":"ok"}\n'
        env = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "example"}
        for outcome in ("success", "stopped"):
            with (
                self.subTest(outcome=outcome),
                patch.dict(os.environ, env, clear=True),
                patch(
                    "hermes_codex_router.external_runtime.pwd.getpwuid",
                    return_value=__import__("types").SimpleNamespace(pw_dir="/home/example"),
                ),
                patch("hermes_codex_router.external_runtime.validate_host_route_settings"),
                patch.object(adapter, "_verified_claude_argv", side_effect=lambda argv, **_: argv),
                patch.object(
                    adapter,
                    "_run_claude_process",
                    return_value=subprocess.CompletedProcess([], 0, output, ""),
                    side_effect=None
                    if outcome == "success"
                    else ExternalTurnInterrupted("claude turn interrupted by user"),
                ) as run,
                patch("hermes_codex_router.external_runtime.reap_cgroup_strays") as reap,
            ):
                if outcome == "success":
                    adapter.run_turn(cwd=root, prompt="hello", claude_sandbox=host)
                    reap.assert_not_called()
                    environment = run.call_args.kwargs["environment"]
                    self.assertEqual(environment["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"], "1")
                else:
                    with self.assertRaises(ExternalTurnInterrupted):
                        adapter.run_turn(cwd=root, prompt="hello", claude_sandbox=host)
                    reap.assert_called_once_with()
