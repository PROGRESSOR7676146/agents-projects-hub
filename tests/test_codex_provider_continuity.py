from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import test_session_adoption_state as state_fixtures
from test_codex_appserver import FakeTransport

from hermes_codex_router.codex_appserver import CodexAppServerClient, CodexMetadataError
from hermes_codex_router.local_transfer import local_resume_command


class ProviderContinuityTests(unittest.TestCase):
    def test_only_explicit_route_extends_metadata_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for route, backend, accepted in (
                (None, "example-proxy", False),
                ("example-proxy", "example-proxy", True),
                ("example-proxy", "openai", True),
                ("example-proxy", "unapproved", False),
            ):
                with self.subTest(route=route, backend=backend):
                    transport = FakeTransport(
                        [
                            {
                                "id": 1,
                                "result": {
                                    "thread": {
                                        "id": "example-thread",
                                        "cwd": directory,
                                        "ephemeral": False,
                                        "modelProvider": backend,
                                        "source": "cli",
                                        "turns": [],
                                        "status": {"type": "notLoaded"},
                                    }
                                },
                            }
                        ]
                    )
                    client = CodexAppServerClient(transport, initialized=True, model_provider=route)
                    if accepted:
                        self.assertEqual(
                            client.read_thread_metadata(
                                thread_id="example-thread", cwd=root
                            ).model_provider,
                            backend,
                        )
                    else:
                        with self.assertRaises(CodexMetadataError):
                            client.read_thread_metadata(thread_id="example-thread", cwd=root)

    def test_resume_pins_route_without_changing_identity_or_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = FakeTransport(
                [
                    {
                        "id": 1,
                        "result": {
                            "thread": {"id": "example-thread"},
                            "cwd": directory,
                            "model": "example-model",
                            "modelProvider": "example-proxy",
                            "approvalPolicy": "on-request",
                            "sandbox": "workspace-write",
                        },
                    }
                ]
            )
            client = CodexAppServerClient(
                transport, initialized=True, model_provider="example-proxy"
            )
            result = client.resume_thread(
                thread_id="example-thread", cwd=Path(directory), model="example-model"
            )
            params = transport.sent[0]["params"]
            self.assertEqual(params["modelProvider"], "example-proxy")
            self.assertEqual(params["approvalPolicy"], "on-request")
            self.assertEqual(result.thread_id, "example-thread")

    def test_local_command_pins_route_and_model_for_old_saved_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            result = local_resume_command(
                "codex",
                None,
                "example-thread",
                Path(directory),
                model_provider="example-proxy",
                model="example-model",
            )
            self.assertIn('model_provider="example-proxy"', result.argv)
            self.assertIn('model="example-model"', result.argv)
            self.assertIn("example-thread", result.argv)

    def test_origin_keeps_actual_provider_across_return_and_restart(self):
        fixture = state_fixtures.SessionAdoptionStateTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        session = fixture.attach(replace(fixture.request, model_provider="example-proxy")).session
        fixture.activate(session)
        self.assertEqual(
            fixture.origins.require(session.session_id).model_provider, "example-proxy"
        )
        self.assertEqual(
            fixture.state.get_session(session.session_id).provider_session_id, "example-thread"
        )
