from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Literal

from hermes_codex_router.codex_appserver import CodexAppServerClient
from hermes_codex_router.codex_failure import (
    UnsupportedCodexPermissionProfileError,
    codex_failure_notice,
    codex_preparation,
)
from tests.test_codex_appserver import FakeTransport


class CodexPermissionProfileTests(unittest.TestCase):
    def test_start_and_resume_refuse_custom_or_malformed_profiles_before_turn(self) -> None:
        profiles = (
            {"id": "example-restrictive", "extends": None},
            {"id": "example-wider", "extends": ":workspace"},
            {"id": ":workspace", "extends": "example-wider"},
            {"id": ":workspace"},
            {"id": ":read-only", "extends": None},
            {"id": ":full-access", "extends": None},
            {"id": "", "extends": None},
            {"id": 1, "extends": None},
            {},
            [],
            "example-private-profile",
            True,
        )
        with tempfile.TemporaryDirectory() as directory:
            for method in ("thread/start", "thread/resume"):
                for profile in profiles:
                    with self.subTest(method=method, profile=profile):
                        client, transport = self.client(
                            directory, {"activePermissionProfile": profile}
                        )
                        with self.assertRaises(UnsupportedCodexPermissionProfileError) as caught:
                            with codex_preparation():
                                self.open_thread(client, method, Path(directory))
                                self.turn(client, Path(directory))
                        self.assertEqual([m["method"] for m in transport.sent], [method])
                        notice = codex_failure_notice(caught.exception)
                        self.assertIn("unsupported permission profile", notice)
                        self.assertIn("No productive provider turn was sent", notice)
                        self.assertNotIn("example-private-profile", notice)
                        self.assertNotIn("Retry after Codex is available", notice)

    def test_old_server_null_and_builtin_workspace_keep_legacy_policy_and_approvals(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for method in ("thread/start", "thread/resume"):
                for profile_fields in (
                    {},
                    {"activePermissionProfile": None},
                    {"activePermissionProfile": {"id": ":workspace", "extends": None}},
                ):
                    for approval in ("on-request", "never"):
                        with self.subTest(method=method, fields=profile_fields, approval=approval):
                            client, transport = self.client(directory, profile_fields, approval)
                            self.open_thread(client, method, Path(directory))
                            self.assertEqual(self.turn(client, Path(directory)), "example-turn")
                            self.assertEqual(
                                [m["method"] for m in transport.sent], [method, "turn/start"]
                            )
                            for message in transport.sent:
                                self.assertEqual(message["params"]["approvalPolicy"], approval)
                                self.assertNotIn("permissions", message["params"])
                            self.assertEqual(
                                transport.sent[-1]["params"]["sandboxPolicy"],
                                {
                                    "type": "workspaceWrite",
                                    "writableRoots": [directory],
                                    "networkAccess": False,
                                },
                            )

    @staticmethod
    def client(
        directory: str, fields: dict, approval: Literal["on-request", "never"] = "on-request"
    ) -> tuple[CodexAppServerClient, FakeTransport]:
        transport = FakeTransport(
            [
                {
                    "id": 1,
                    "result": {
                        "thread": {"id": "example-thread"},
                        "cwd": directory,
                        "approvalPolicy": approval,
                        "sandbox": {"type": "workspaceWrite", "networkAccess": False},
                        **fields,
                    },
                },
                {"id": 2, "result": {"turn": {"id": "example-turn"}}},
            ]
        )
        return CodexAppServerClient(
            transport, initialized=True, approval_policy=approval
        ), transport

    @staticmethod
    def open_thread(client: CodexAppServerClient, method: str, root: Path) -> None:
        if method == "thread/start":
            client.start_thread(cwd=root, model="example-model", project_id="example-project")
        else:
            client.resume_thread(thread_id="example-thread", cwd=root, model="example-model")

    @staticmethod
    def turn(client: CodexAppServerClient, root: Path) -> str:
        return client.start_turn(
            thread_id="example-thread",
            cwd=root,
            text="Example request",
            model="example-model",
            effort="low",
        )
