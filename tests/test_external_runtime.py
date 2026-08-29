from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.external_runtime import ExternalCliAdapter


class ExternalRuntimeTests(unittest.TestCase):
    def test_gemini_uses_structured_safe_non_yolo_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            adapter = ExternalCliAdapter("gemini")
            argv = adapter.build_argv(
                cwd=Path(directory),
                prompt="inspect; touch /tmp/no",
                session_id="session-1",
                model="gemini-model",
            )
        self.assertIn("--output-format", argv)
        self.assertIn("default", argv)
        self.assertNotIn("--yolo", argv)
        self.assertNotIn("yolo", argv)
        self.assertIn("inspect; touch /tmp/no", argv)

    def test_opencode_parses_json_events_without_auto_approval(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            output = (
                '{"sessionID":"ses_1","model":"provider/model"}\n'
                '{"part":{"text":"Visible answer"}}\n'
            )
            return subprocess.CompletedProcess(argv, 0, output, "")

        with tempfile.TemporaryDirectory() as directory:
            result = ExternalCliAdapter("opencode", run=fake_run).run_turn(
                cwd=Path(directory), prompt="hello"
            )
        self.assertEqual(result.provider_session_id, "ses_1")
        self.assertEqual(result.text, "Visible answer")
        self.assertNotIn("--auto", calls[0])

    def test_opencode_effort_is_passed_as_provider_variant(self) -> None:
        adapter = ExternalCliAdapter("opencode", executable="/usr/bin/opencode")
        argv = adapter.build_argv(
            cwd=Path.cwd(),
            prompt="Inspect",
            model="opencode-go/glm-5.3",
            effort="high",
        )
        self.assertIn("--variant", argv)
        self.assertEqual(argv[argv.index("--variant") + 1], "high")

    def test_gemini_profile_is_isolated_in_child_environment(self) -> None:
        environments: list[dict[str, str]] = []

        def fake_run(argv: tuple[str, ...], **kwargs: object) -> subprocess.CompletedProcess[str]:
            environments.append(dict(kwargs["env"]))  # type: ignore[arg-type]
            return subprocess.CompletedProcess(argv, 0, '{"response":"ok"}', "")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = root / "gemini-account-a"
            profile.mkdir()
            ExternalCliAdapter("gemini", runtime_home=profile, run=fake_run).run_turn(
                cwd=root, prompt="hello"
            )
        self.assertEqual(environments[0]["GEMINI_CLI_HOME"], str(profile.resolve()))

    def test_antigravity_uses_sandboxed_plan_mode_and_resumes_conversation(self) -> None:
        calls: list[tuple[str, ...]] = []

        def fake_run(argv: tuple[str, ...], **_: object) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            output = '{"conversation_id":"conv-1","status":"SUCCESS","response":"Visible answer"}'
            return subprocess.CompletedProcess(argv, 0, output, "")

        with tempfile.TemporaryDirectory() as directory:
            result = ExternalCliAdapter("antigravity", executable="agy", run=fake_run).run_turn(
                cwd=Path(directory), prompt="hello", session_id="conv-1"
            )
        self.assertEqual(result.provider_session_id, "conv-1")
        self.assertEqual(result.text, "Visible answer")
        self.assertIn("--sandbox", calls[0])
        self.assertIn("plan", calls[0])
        self.assertIn("--conversation", calls[0])
        self.assertNotIn("--dangerously-skip-permissions", calls[0])

    def test_antigravity_effort_is_encoded_in_selected_model(self) -> None:
        adapter = ExternalCliAdapter("antigravity", executable="/usr/bin/agy")
        argv = adapter.build_argv(
            cwd=Path.cwd(),
            prompt="Inspect",
            model="gemini-3.7-flash",
            effort="medium",
        )
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.7-flash-medium")


if __name__ == "__main__":
    unittest.main()
