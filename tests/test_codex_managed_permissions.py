from __future__ import annotations

import copy
import tempfile
import unittest
from collections import deque
from pathlib import Path

from hermes_codex_router.codex_appserver import CodexAppServerClient, CodexTurnError, RpcError
from hermes_codex_router.codex_permissions import (
    CodexPermissionProfileError,
    validate_permission_profile_id,
    verify_managed_selection,
)
from tests.test_codex_appserver import FakeTransport

PROFILE = "example-project-policy"


class ManagedCodexPermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)

    def metadata(self, approval: str = "on-request") -> dict:
        return {
            "thread": {"id": "example-thread"},
            "cwd": str(self.root),
            "modelProvider": "openai",
            "approvalPolicy": approval,
            "approvalsReviewer": "user",
            "activePermissionProfile": {"id": PROFILE, "extends": ":workspace"},
            "sandbox": {
                "type": "workspaceWrite",
                "networkAccess": False,
                "writableRoots": [],
                "excludeTmpdirEnvVar": True,
                "excludeSlashTmp": True,
            },
        }

    def transport(
        self, metadata: dict | None = None, *, tail: tuple[dict, ...] | list[dict] = ()
    ) -> FakeTransport:
        return FakeTransport(
            [
                {
                    "id": 1,
                    "result": {"data": [{"id": PROFILE, "allowed": True}], "nextCursor": None},
                },
                {
                    "id": 2,
                    "result": {
                        "requirements": {
                            "defaultPermissions": PROFILE,
                            "allowedPermissionProfiles": {PROFILE: True, ":workspace": False},
                        }
                    },
                },
                {"id": 3, "result": self.metadata() if metadata is None else metadata},
                {"id": 4, "result": {"turn": {"id": "example-turn"}}},
                *tail,
            ]
        )

    def client(
        self, transport: FakeTransport, *, approval: str = "on-request"
    ) -> CodexAppServerClient:
        return CodexAppServerClient(
            transport, initialized=True, approval_policy=approval, permission_profile=PROFILE
        )

    def open_thread(self, client: CodexAppServerClient, *, resume: bool = False) -> None:
        if resume:
            client.resume_thread(thread_id="example-thread", cwd=self.root, model="example-model")
        else:
            client.start_thread(cwd=self.root, model="example-model", project_id="example-project")

    def start_turn(
        self,
        client: CodexAppServerClient,
        *,
        thread: str = "example-thread",
        root: Path | None = None,
    ) -> str:
        return client.start_turn(
            thread_id=thread,
            cwd=self.root if root is None else root,
            text="Example request",
            model="example-model",
            effort="low",
        )

    def test_profile_id_cannot_select_builtin_or_unsafe_configuration(self) -> None:
        self.assertEqual(validate_permission_profile_id(PROFILE), PROFILE)
        self.assertIsNone(validate_permission_profile_id(None))
        for value in (
            "",
            ":workspace",
            ":full-access",
            "../example",
            "example\npolicy",
            True,
            1,
            "x" * 65,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_permission_profile_id(value)

    def test_start_resume_and_turn_select_exact_profile_without_legacy_overrides(self) -> None:
        for resume in (False, True):
            for approval in ("on-request", "never"):
                with self.subTest(resume=resume, approval=approval):
                    transport = self.transport(self.metadata(approval))
                    client = self.client(transport, approval=approval)
                    self.open_thread(client, resume=resume)
                    self.assertEqual(self.start_turn(client), "example-turn")
                    self.assertEqual(
                        [message["method"] for message in transport.sent],
                        [
                            "permissionProfile/list",
                            "configRequirements/read",
                            "thread/resume" if resume else "thread/start",
                            "turn/start",
                        ],
                    )
                    self.assertEqual(transport.sent[0]["params"]["cwd"], str(self.root))
                    self.assertTrue(
                        all(timeout is not None for timeout in transport.receive_timeouts[:2])
                    )
                    for message in transport.sent[2:]:
                        params = message["params"]
                        self.assertEqual(params["permissions"], PROFILE)
                        self.assertEqual(params["approvalPolicy"], approval)
                        self.assertEqual(params["approvalsReviewer"], "user")
                        self.assertNotIn("sandbox", params)
                        self.assertNotIn("sandboxPolicy", params)

    def test_missing_disallowed_ambiguous_and_unmanaged_profiles_never_create_threads(self) -> None:
        replacements = (
            (0, {"data": []}),
            (0, {"data": [{"id": PROFILE, "allowed": False}]}),
            (
                0,
                {
                    "data": [
                        {"id": PROFILE, "allowed": True},
                        {"id": ":full-access", "allowed": True},
                    ]
                },
            ),
            (0, {"data": [{"id": PROFILE, "allowed": True}] * 2}),
            (0, {"data": [{"id": PROFILE, "allowed": 1}]}),
            (1, {"requirements": None}),
            (
                1,
                {
                    "requirements": {
                        "defaultPermissions": "example-other",
                        "allowedPermissionProfiles": {PROFILE: True},
                    }
                },
            ),
            (
                1,
                {
                    "requirements": {
                        "defaultPermissions": PROFILE,
                        "allowedPermissionProfiles": {PROFILE: True, ":workspace": True},
                    }
                },
            ),
        )
        for index, response in replacements:
            with self.subTest(index=index, response=response):
                transport = self.transport()
                transport.incoming[index]["result"] = copy.deepcopy(response)
                with self.assertRaises(CodexPermissionProfileError):
                    self.open_thread(self.client(transport))
                self.assertTrue(
                    all(
                        message["method"] in {"permissionProfile/list", "configRequirements/read"}
                        for message in transport.sent
                    )
                )

    def test_wrong_selection_parent_approval_reviewer_root_and_provider_prevent_turn(self) -> None:
        fields = (
            {"activePermissionProfile": None},
            {"activePermissionProfile": {"id": "example-other", "extends": ":workspace"}},
            {"activePermissionProfile": {"id": PROFILE, "extends": ":full-access"}},
            {"activePermissionProfile": {"id": PROFILE}},
            {"approvalPolicy": "never"},
            {"approvalsReviewer": "auto_review"},
            {"sandbox": {"type": "dangerFullAccess"}},
            {"sandbox": {**self.metadata()["sandbox"], "networkAccess": True}},
            {"sandbox": {**self.metadata()["sandbox"], "writableRoots": [str(self.root.parent)]}},
            {"sandbox": {**self.metadata()["sandbox"], "excludeSlashTmp": False}},
            {"sandbox": {**self.metadata()["sandbox"], "excludeTmpdirEnvVar": False}},
            {"cwd": str(self.root.parent)},
            {"modelProvider": "example-other"},
        )
        for resume in (False, True):
            for field in fields:
                with self.subTest(resume=resume, field=field):
                    transport = self.transport({**self.metadata(), **field})
                    client = self.client(transport)
                    with self.assertRaises((CodexPermissionProfileError, RpcError)):
                        self.open_thread(client, resume=resume)
                        self.start_turn(client)
                    self.assertNotIn(
                        "turn/start", [message["method"] for message in transport.sent]
                    )

    def test_managed_turn_requires_exact_prepared_thread_and_root(self) -> None:
        transport = self.transport()
        client = self.client(transport)
        with self.assertRaises(CodexPermissionProfileError):
            self.start_turn(client)
        self.assertEqual(transport.sent, [])
        self.open_thread(client)
        for thread, root in (("example-other", self.root), ("example-thread", self.root.parent)):
            with (
                self.subTest(thread=thread, root=root),
                self.assertRaises(CodexPermissionProfileError),
            ):
                self.start_turn(client, thread=thread, root=root)
        self.assertNotIn("turn/start", [message["method"] for message in transport.sent])

    def test_opaque_managed_parent_confirms_selection_without_definition_attestation(self) -> None:
        metadata = self.metadata()
        metadata["activePermissionProfile"]["extends"] = None
        transport = self.transport(metadata)
        client = self.client(transport)
        self.open_thread(client)
        self.assertEqual(self.start_turn(client), "example-turn")

    def test_policy_drift_after_acceptance_interrupts_and_retains_uncertainty(self) -> None:
        for change in (
            {"activePermissionProfile": None},
            {"sandboxPolicy": {**self.metadata()["sandbox"], "networkAccess": True}},
            {"sandboxPolicy": {**self.metadata()["sandbox"], "writableRoots": ["/"]}},
        ):
            settings = self.metadata()
            settings["sandboxPolicy"] = settings.pop("sandbox")
            settings.update(change)
            transport = self.transport(
                tail=[
                    {
                        "method": "thread/settings/updated",
                        "params": {"threadId": "example-thread", "threadSettings": settings},
                    },
                    {"id": 5, "result": {}},
                ]
            )
            client = self.client(transport)
            self.open_thread(client)
            self.start_turn(client)
            with self.subTest(change=change), self.assertRaises(CodexTurnError):
                client.wait_for_turn("example-turn")
            self.assertEqual(transport.sent[-1]["method"], "turn/interrupt")
            self.assertEqual(
                transport.sent[-1]["params"],
                {"threadId": "example-thread", "turnId": "example-turn"},
            )

    def test_profile_pagination_is_bounded_and_malformed_or_cyclic_metadata_refuses(self) -> None:
        for cursor in (True, "", "x" * 2049, "example-cycle"):
            calls = []

            def request(method, params, *, deadline=None):
                calls.append((method, params, deadline))
                return {"data": [], "nextCursor": cursor}

            with self.subTest(cursor=cursor), self.assertRaises(CodexPermissionProfileError):
                verify_managed_selection(request, PROFILE, self.root)
            self.assertLessEqual(len(calls), 2)
            self.assertTrue(all(call[2] is not None for call in calls))
        calls = []

        def too_many(method, params, *, deadline=None):
            calls.append(params)
            return {"data": [], "nextCursor": f"example-page-{len(calls)}"}

        with self.assertRaises(CodexPermissionProfileError):
            verify_managed_selection(too_many, PROFILE, self.root)
        self.assertEqual(len(calls), 10)

        def timed_out(method, params, *, deadline=None):
            raise TimeoutError("fictional metadata timeout")

        with self.assertRaises(CodexPermissionProfileError):
            verify_managed_selection(timed_out, PROFILE, self.root)

    def test_drift_before_acceptance_is_reported_after_acceptance_with_interrupt(self) -> None:
        transport = self.transport(tail=[{"id": 5, "result": {}}])
        transport.incoming.insert(
            3,
            {
                "method": "thread/settings/updated",
                "params": {
                    "threadId": "example-thread",
                    "threadSettings": {**self.metadata(), "approvalsReviewer": "auto_review"},
                },
            },
        )
        client = self.client(transport)
        self.open_thread(client)
        self.assertEqual(self.start_turn(client), "example-turn")
        with self.assertRaises(CodexTurnError):
            client.wait_for_turn("example-turn")
        self.assertEqual(transport.sent[-1]["method"], "turn/interrupt")

    def test_settings_drift_during_thread_preparation_refuses_before_turn(self) -> None:
        for resume in (False, True):
            transport = self.transport()
            transport.incoming.insert(
                2,
                {
                    "method": "thread/settings/updated",
                    "params": {
                        "threadId": "example-thread",
                        "threadSettings": {**self.metadata(), "approvalsReviewer": "auto_review"},
                    },
                },
            )
            with self.subTest(resume=resume), self.assertRaises(CodexPermissionProfileError):
                self.open_thread(self.client(transport), resume=resume)
            self.assertNotIn("turn/start", [message["method"] for message in transport.sent])

    def test_settings_for_another_thread_do_not_change_selected_policy(self) -> None:
        transport = self.transport(
            tail=[
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "example-thread",
                        "turn": {"id": "example-turn", "status": "completed"},
                    },
                }
            ]
        )
        transport.incoming.insert(
            3,
            {
                "method": "thread/settings/updated",
                "params": {"threadId": "example-other-thread", "threadSettings": {}},
            },
        )
        client = self.client(transport)
        self.open_thread(client)
        self.start_turn(client)
        client.wait_for_turn("example-turn")
        self.assertNotIn("turn/interrupt", [message["method"] for message in transport.sent])

    def test_failed_preparation_clears_settings_before_client_reuse(self) -> None:
        for resume in (False, True):
            for failure in ("rpc", "cwd", "approval", "sandbox"):
                with self.subTest(resume=resume, failure=failure):
                    transport = self.transport()
                    if failure == "rpc":
                        transport.incoming[2] = {
                            "id": 3,
                            "error": {"code": -1, "message": "Example failure"},
                        }
                    else:
                        transport.incoming[2]["result"][
                            failure if failure != "approval" else "approvalPolicy"
                        ] = str(self.root.parent) if failure == "cwd" else "unsafe"
                    transport.incoming.insert(
                        2,
                        {
                            "method": "thread/settings/updated",
                            "params": {
                                "threadId": "example-thread",
                                "threadSettings": self.metadata(),
                            },
                        },
                    )
                    client = self.client(transport)
                    with self.assertRaises(RpcError):
                        self.open_thread(client, resume=resume)
                    transport.incoming = deque(
                        [
                            *(
                                {
                                    "method": "thread/settings/updated",
                                    "params": {
                                        "threadId": "example-other-thread",
                                        "threadSettings": {},
                                    },
                                }
                                for _ in range(40)
                            ),
                            {"id": 4, "result": {"rateLimits": {}}},
                        ]
                    )
                    client.read_rate_limits()
                    self.assertFalse(client._permission_preparing)
                    self.assertEqual(client._preparation_settings, [])

    def test_resume_preparation_does_not_count_unrelated_thread_settings(self) -> None:
        transport = self.transport()
        for _ in range(40):
            transport.incoming.insert(
                2,
                {
                    "method": "thread/settings/updated",
                    "params": {"threadId": "example-other-thread", "threadSettings": {}},
                },
            )
        client = self.client(transport)
        self.open_thread(client, resume=True)
        self.assertEqual(self.start_turn(client), "example-turn")


if __name__ == "__main__":
    unittest.main()
