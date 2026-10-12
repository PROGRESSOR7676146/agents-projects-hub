"""Reject counterfeit MCP evidence before the optional native custody rehearsal."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.codex_native_mcp_consent import SyntheticMcpConsent
from tests.codex_native_mcp_evidence import (
    McpEvidenceError,
    direct_mcp_probe,
    proven_command_custody,
    proven_mcp_probe,
)
from tests.codex_native_mcp_server import SERVER, decode_json, dispatch, probe, trusted_root
from tests.codex_native_profile_actor import select_tool
from tests.codex_native_profile_fixture import NativeProfileFixture, NativeProfileFixtureError


def payload() -> dict:
    probe = {
        "project_read": True,
        "project_write": True,
        "authority_read": False,
        "authority_symlink_read": False,
        "authority_write": False,
        "git_write": False,
    }
    return {"version": 1, "nonce": "example-nonce-1", "direct": probe, "child": dict(probe)}


def tool_result() -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload())}],
        "structuredContent": payload(),
    }


def evidence() -> dict:
    return {
        "thread_id": "example-thread",
        "turn_id": "example-turn",
        "status": "completed",
        "items": [
            {
                "thread_id": "example-thread",
                "turn_id": "example-turn",
                "item": {
                    "id": "example-item",
                    "type": "mcpToolCall",
                    "status": "completed",
                    "server": SERVER,
                    "tool": "probe",
                    "arguments": {"nonce": "example-nonce-1"},
                    "result": tool_result(),
                    "error": None,
                },
            }
        ],
    }


class McpEvidenceTests(unittest.TestCase):
    def test_matching_completed_current_item_and_both_payloads_are_required(self):
        self.assertEqual(proven_mcp_probe(evidence(), "example-nonce-1"), payload())
        self.assertEqual(direct_mcp_probe(tool_result(), "example-nonce-1", 0, 0), payload())

    def test_identity_duplicates_status_error_and_stale_nonce_cannot_prove_execution(self):
        mutations = (
            lambda x: x.update(status="failed"),
            lambda x: x.update(items=[]),
            lambda x: x["items"].extend(copy.deepcopy(x["items"])),
            lambda x: x["items"][0].update(thread_id="example-other"),
            lambda x: x["items"][0].update(turn_id="example-old"),
            lambda x: x["items"][0]["item"].update(status="failed"),
            lambda x: x["items"][0]["item"].update(server="example-other"),
            lambda x: x["items"][0]["item"].update(tool="other"),
            lambda x: x["items"][0]["item"].update(error={"message": "example"}),
            lambda x: x["items"][0]["item"].update(arguments={"nonce": "example-nonce-2"}),
            lambda x: x["items"][0]["item"].update(result=None),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                result = evidence()
                mutate(result)
                with self.assertRaises(McpEvidenceError):
                    proven_mcp_probe(result, "example-nonce-1")

    def test_malformed_partial_disagreeing_and_stale_payloads_are_rejected(self):
        for value in (True, 2, None, "1"):
            result = tool_result()
            result["structuredContent"]["version"] = value
            with self.subTest(version=value), self.assertRaises(McpEvidenceError):
                direct_mcp_probe(result, "example-nonce-1", 0, 0)
        for mutate in (
            lambda x: x["structuredContent"].update(nonce="example-nonce-2"),
            lambda x: x["structuredContent"]["direct"].update(project_write=1),
            lambda x: x["structuredContent"]["child"].pop("authority_write"),
            lambda x: x.update(content=[]),
            lambda x: x["content"].append(copy.deepcopy(x["content"][0])),
            lambda x: x["content"][0].update(text="{invalid}"),
        ):
            result = tool_result()
            mutate(result)
            with self.assertRaises(McpEvidenceError):
                direct_mcp_probe(result, "example-nonce-1", 0, 0)

    def test_direct_error_and_any_responses_activity_are_rejected(self):
        for error in (True, "false", 0):
            with self.subTest(error=error), self.assertRaises(McpEvidenceError):
                direct_mcp_probe({**tool_result(), "isError": error}, "example-nonce-1", 0, 0)
        for before, after in ((0, 1), (1, 0), (True, True), (-1, -1)):
            with self.subTest(before=before, after=after), self.assertRaises(McpEvidenceError):
                direct_mcp_probe(tool_result(), "example-nonce-1", before, after)

    def test_duplicate_json_fields_and_malformed_containers_fail_with_fixed_error(self):
        for text in (
            json.dumps(payload()).replace('"nonce":', '"nonce":"example-nonce-2","nonce":'),
            json.dumps(payload()).replace(
                '"project_read": true', '"project_read":false,"project_read":true'
            ),
        ):
            result = tool_result()
            result["content"][0]["text"] = text
            with self.assertRaises(McpEvidenceError):
                direct_mcp_probe(result, "example-nonce-1", 0, 0)
            with self.assertRaises(ValueError):
                decode_json(text)
        for result in (None, [], "example"):
            with self.assertRaises(McpEvidenceError):
                direct_mcp_probe(result, "example-nonce-1", 0, 0)
            with self.assertRaises(McpEvidenceError):
                proven_mcp_probe(result, "example-nonce-1")
        for rows in (None, {}, [None], [{"item": None}]):
            with self.assertRaises(McpEvidenceError):
                proven_mcp_probe({**evidence(), "items": rows}, "example-nonce-1")


class HostFixturePathTests(unittest.TestCase):
    def test_independent_observation_rejects_symlink_and_nonregular_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fixture = object.__new__(NativeProfileFixture)
            fixture.project = base / "example-project"
            fixture.project.mkdir()
            target = base / "example-sentinel"
            target.write_text("fictional untouched sentinel")
            entry = fixture.project / "example-write"
            self.assertIsNone(fixture.project_file_bytes("example-write"))
            entry.symlink_to(target)
            with self.assertRaises(NativeProfileFixtureError):
                fixture.project_file_bytes("example-write")
            entry.unlink()
            os.mkfifo(entry)
            with self.assertRaises(NativeProfileFixtureError):
                fixture.project_file_bytes("example-write")
            self.assertEqual(target.read_text(), "fictional untouched sentinel")

    def test_preparation_never_follows_replaced_project_git_or_head(self):
        for component in ("project", "git", "head"):
            with self.subTest(component=component), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                fixture = object.__new__(NativeProfileFixture)
                fixture.base = base
                fixture.project = base / "example-project"
                fixture.project.mkdir()
                (fixture.project / ".git").mkdir()
                fixture.case_id = 0
                fixture.events = []
                outside = base / "example-outside"
                outside.mkdir()
                target = outside / "HEAD"
                target.write_text("fictional untouched sentinel")
                if component == "project":
                    (fixture.project / ".git").rmdir()
                    fixture.project.rmdir()
                    (outside / ".git").mkdir()
                    target.rename(outside / ".git" / "HEAD")
                    target = outside / ".git" / "HEAD"
                    fixture.project.symlink_to(outside, target_is_directory=True)
                elif component == "git":
                    (fixture.project / ".git").rmdir()
                    (fixture.project / ".git").symlink_to(outside, target_is_directory=True)
                else:
                    (fixture.project / ".git" / "HEAD").symlink_to(target)
                with (
                    patch.object(fixture, "_send") as send,
                    patch.object(fixture, "_take", return_value={"case": "example-case-1"}),
                    self.assertRaises(NativeProfileFixtureError),
                ):
                    fixture.prepare_turn_case()
                send.assert_not_called()
                self.assertEqual(target.read_text(), "fictional untouched sentinel")


class SyntheticConsentTests(unittest.TestCase):
    def test_decision_record_binds_native_request_to_exact_thread_and_turn(self):
        fixture = object.__new__(NativeProfileFixture)
        fixture.events = []
        request, _ = self.case()
        with patch.object(fixture, "_send") as send:
            fixture._answer_mcp(request, {"action": "accept", "content": {}})
        send.assert_called_once_with({"id": 1, "result": {"action": "accept", "content": {}}})
        self.assertEqual(
            fixture.events,
            [
                {
                    "fixture_event": "mcp_consent_decision",
                    "request_id": 1,
                    "thread_id": "example-thread",
                    "turn_id": "example-turn",
                    "action": "accept",
                }
            ],
        )

    def test_cleanup_detaches_pending_state_even_when_decline_pipe_is_broken(self):
        fixture = object.__new__(NativeProfileFixture)
        fixture.synthetic_consent = SyntheticMcpConsent("example-thread", "example-nonce-1")
        fixture.pending_mcp_request = {"id": 1}
        with patch.object(fixture, "_send", side_effect=BrokenPipeError):
            fixture._clear_consent()
        self.assertIsNone(fixture.synthetic_consent)
        self.assertIsNone(fixture.pending_mcp_request)

    def case(self):
        request = {
            "id": 1,
            "method": "mcpServer/elicitation/request",
            "params": {
                "threadId": "example-thread",
                "turnId": "example-turn",
                "serverName": SERVER,
                "mode": "form",
                "requestedSchema": {"type": "object", "properties": {}},
                "_meta": {
                    "codex_approval_kind": "mcp_tool_call",
                    "tool_params": {"nonce": "example-nonce-1"},
                    "persist": ["always", "session"],
                },
            },
        }
        item = copy.deepcopy(evidence()["items"][0]["item"])
        item["status"] = "inProgress"
        event = {
            "method": "item/started",
            "params": {"threadId": "example-thread", "turnId": "example-turn", "item": item},
        }
        return request, [event]

    def test_explicit_case_defers_until_native_acceptance_and_never_persists_grant(self):
        consent = SyntheticMcpConsent("example-thread", "example-nonce-1")
        request, events = self.case()
        self.assertIsNone(consent.answer(request, events))
        self.assertFalse(consent.consumed)
        consent.turn_id = "example-turn"
        self.assertEqual(consent.answer(request, events), {"action": "accept", "content": {}})
        self.assertEqual(consent.answer(request, events), {"action": "decline"})

    def test_wrong_identity_schema_arguments_or_ambiguous_item_consumes_case_as_denied(self):
        for mutate in (
            lambda r, e: r["params"].update(threadId="example-other"),
            lambda r, e: r["params"].update(turnId=None),
            lambda r, e: r["params"].update(serverName="example-other"),
            lambda r, e: r["params"].update(mode="url"),
            lambda r, e: r["params"].update(requestedSchema={}),
            lambda r, e: r["params"]["_meta"].update(codex_approval_kind="other"),
            lambda r, e: r["params"]["_meta"].update(tool_params={"nonce": "example-nonce-2"}),
            lambda r, e: e[0]["params"]["item"].update(tool="other"),
            lambda r, e: e.clear(),
            lambda r, e: e.extend(copy.deepcopy(e)),
        ):
            consent = SyntheticMcpConsent("example-thread", "example-nonce-1", "example-turn")
            request, events = self.case()
            mutate(request, events)
            self.assertEqual(consent.answer(request, events), {"action": "decline"})
            self.assertTrue(consent.consumed)

    def test_deferred_consent_cannot_accept_after_matching_item_or_turn_completed(self):
        for turn_terminal in (False, True):
            request, events = self.case()
            if turn_terminal:
                events.append(
                    {
                        "method": "turn/completed",
                        "params": {"threadId": "example-thread", "turn": {"id": "example-turn"}},
                    }
                )
            else:
                events.append({**copy.deepcopy(events[0]), "method": "item/completed"})
            consent = SyntheticMcpConsent("example-thread", "example-nonce-1", "example-turn")
            self.assertEqual(consent.answer(request, events), {"action": "decline"})


class FixedMcpServerTests(unittest.TestCase):
    def test_exact_flat_or_namespaced_advertisement_rejects_absence_and_ambiguity(self):
        function = {"type": "function", "name": "probe"}
        namespace = {"type": "namespace", "name": "mcp__example_custody", "tools": [function]}
        self.assertEqual(select_tool([namespace], "mcp", SERVER), (function, namespace["name"]))
        flat = {"type": "function", "name": "mcp__example_custody__probe"}
        self.assertEqual(select_tool([flat], "mcp", SERVER), (flat, None))
        for tools in (
            [],
            [namespace, namespace],
            [namespace, flat],
            [{**namespace, "name": "mcp__other"}],
        ):
            with self.assertRaises(ValueError):
                select_tool(tools, "mcp", SERVER)

    def test_trusted_root_rejects_missing_declaration_cwd_alias_and_wrong_authority(self):
        with tempfile.TemporaryDirectory(prefix="example-mcp-root-") as temporary:
            base = Path(temporary)
            root = base / "example-project"
            root.mkdir()
            (root / ".git").mkdir()
            (root / "visible").write_text("fictional")
            key = base / "example-authority.key"
            key.write_text("fictional")
            (root / "private-link").symlink_to(key)
            alias = base / "example-alias"
            alias.symlink_to(root, target_is_directory=True)
            for declared, cwd in (("", root), (str(root), base), (str(alias), root)):
                with (
                    patch.dict(os.environ, {"EXAMPLE_MCP_PROJECT": declared}),
                    patch.object(Path, "cwd", return_value=cwd),
                ):
                    with self.assertRaises(RuntimeError):
                        trusted_root()
            with (
                patch.dict(os.environ, {"EXAMPLE_MCP_PROJECT": str(root)}),
                patch.object(Path, "cwd", return_value=root),
            ):
                self.assertEqual(trusted_root(), root)
                (root / "private-link").unlink()
                (root / "private-link").symlink_to(root / "visible")
                with self.assertRaises(RuntimeError):
                    trusted_root()

    @unittest.skipUnless(Path("/usr/bin/python3").is_file(), "fixed namespace Python unavailable")
    def test_fixed_direct_and_child_probe_are_executable_and_observe_same_controls(self):
        with tempfile.TemporaryDirectory(prefix="example-mcp-unit-") as temporary:
            base = Path(temporary)
            root = base / "example-project"
            root.mkdir()
            (root / ".git").mkdir()
            (root / "visible").write_text("fictional visible sentinel")
            key = base / "example-authority.key"
            key.write_text("fictional authority sentinel")
            (root / "private-link").symlink_to(key)
            value = probe(root, "example-nonce-1")
            self.assertEqual(value["direct"], value["child"])
            self.assertTrue(all(value["direct"].values()))

    def test_advertisement_is_one_nonce_only_tool_and_no_resources(self):
        response = dispatch({"id": 1, "method": "tools/list"}, None)
        assert response is not None
        listing = response["result"]
        self.assertEqual([tool["name"] for tool in listing["tools"]], ["probe"])
        schema = listing["tools"][0]["inputSchema"]
        self.assertEqual(schema["required"], ["nonce"])
        self.assertEqual(set(schema["properties"]), {"nonce"})
        self.assertIs(schema["additionalProperties"], False)
        response = dispatch({"id": 2, "method": "resources/list"}, None)
        assert response is not None
        self.assertEqual(response["result"], {"resources": []})

    def test_unknown_tool_extra_arguments_and_invalid_nonce_do_not_reach_probe(self):
        for params in (
            {"name": "shell", "arguments": {"nonce": "example-nonce-1"}},
            {"name": "probe", "arguments": {"nonce": "example-nonce-1", "path": "/tmp"}},
            {"name": "probe", "arguments": {"nonce": "../example"}},
            {"name": "probe", "arguments": {"nonce": 1}},
            {"name": "probe", "arguments": {}},
        ):
            with self.subTest(params=params):
                response = dispatch({"id": 1, "method": "tools/call", "params": params}, None)
                assert response is not None
                self.assertEqual(response["error"]["code"], -32602)
        response = dispatch({"id": 1, "method": "sampling/createMessage"}, None)
        assert response is not None
        self.assertEqual(response["error"]["code"], -32601)


class NativeMcpCustodyTests(unittest.TestCase):
    def setUp(self):
        executable = os.environ.get("HUB_NATIVE_CODEX_FIXTURE_EXECUTABLE")
        if not executable:
            if os.environ.get("HUB_REQUIRE_NATIVE_CODEX_PROFILE_TESTS") == "1":
                self.fail("required offline native MCP fixture executable is unavailable")
            self.skipTest("explicit offline native MCP fixture executable is unavailable")
        assert executable is not None
        self.fixture = self.enterContext(NativeProfileFixture(Path(executable), mcp=True))

    def assertObservedExposure(self, value):
        # Passing evidence checks record exposure; they never establish custody.
        self.assertEqual(value["direct"], value["child"])
        self.assertTrue(all(value["direct"].values()))

    def assertFreshConsent(self, result, event_offset):
        events = self.fixture.events[event_offset:]
        requests = [
            event
            for event in events
            if event.get("method") == "mcpServer/elicitation/request"
            and event.get("params", {}).get("threadId") == result["thread_id"]
            and event.get("params", {}).get("turnId") == result["turn_id"]
        ]
        self.assertEqual(len(requests), 1)
        decisions = [
            event for event in events if event.get("fixture_event") == "mcp_consent_decision"
        ]
        self.assertEqual(
            decisions,
            [
                {
                    "fixture_event": "mcp_consent_decision",
                    "request_id": requests[0]["id"],
                    "thread_id": result["thread_id"],
                    "turn_id": result["turn_id"],
                    "action": "accept",
                }
            ],
        )

    def test_direct_rpc_uses_no_responses_and_compares_native_command_descendant(self):
        thread = self.fixture.start_thread()["thread"]["id"]
        before = self.fixture.responses_count()
        result = self.fixture.rpc(
            "mcpServer/tool/call",
            {
                "threadId": thread,
                "server": SERVER,
                "tool": "probe",
                "arguments": {"nonce": "example-nonce-1"},
            },
        )
        after = self.fixture.responses_count()
        self.assertObservedExposure(direct_mcp_probe(result, "example-nonce-1", before, after))
        command = self.fixture.turn(thread, kind="custody_command")
        self.assertEqual(
            proven_command_custody(command),
            {
                "project_read": True,
                "project_write": True,
                "authority_read": False,
                "authority_symlink_read": False,
                "authority_write": False,
                "git_write": False,
            },
        )

    def test_current_native_mcp_call_and_exact_resume_observe_exposure_with_fresh_nonce(self):
        thread = self.fixture.start_thread()["thread"]["id"]
        event_offset = len(self.fixture.events)
        result = self.fixture.turn(
            thread, kind="mcp", nonce="example-nonce-1", synthetic_consent=True
        )
        self.assertObservedExposure(proven_mcp_probe(result, "example-nonce-1"))
        self.assertFreshConsent(result, event_offset)
        self.fixture.prepare_turn_case()  # Repair fictional HEAD before native restart/resume.
        self.fixture.restart_native()
        self.assertEqual(self.fixture.resume_thread(thread)["thread"]["id"], thread)
        event_offset = len(self.fixture.events)
        result = self.fixture.turn(
            thread, kind="mcp", nonce="example-nonce-2", synthetic_consent=True
        )
        self.assertObservedExposure(proven_mcp_probe(result, "example-nonce-2"))
        self.assertFreshConsent(result, event_offset)
        self.assertIsNone(self.fixture.synthetic_consent)

    def test_unarmed_native_mcp_call_is_denied_without_exposure_evidence(self):
        thread = self.fixture.start_thread()["thread"]["id"]
        self.assertUnarmedDenied(
            self.fixture.turn(thread, kind="mcp", nonce="example-nonce-1"), "example-nonce-1", 0
        )

    def test_accept_does_not_suppress_fresh_decline_in_same_native_process(self):
        thread = self.fixture.start_thread()["thread"]["id"]
        offset = len(self.fixture.events)
        result = self.fixture.turn(
            thread, kind="mcp", nonce="example-nonce-1", synthetic_consent=True
        )
        self.assertObservedExposure(proven_mcp_probe(result, "example-nonce-1"))
        self.assertFreshConsent(result, offset)
        offset = len(self.fixture.events)
        self.assertUnarmedDenied(
            self.fixture.turn(thread, kind="mcp", nonce="example-nonce-2"),
            "example-nonce-2",
            offset,
        )

    def assertUnarmedDenied(self, result, nonce, event_offset):
        self.assertIsNone(self.fixture.project_file_bytes("example-write"))
        self.assertEqual(
            self.fixture.project_file_bytes(".git", "HEAD"), b"ref: refs/heads/example\n"
        )
        self.assertEqual(
            (self.fixture.base / "example-authority.key").read_bytes(),
            b"fictional sentinel; no real credential",
        )
        with self.assertRaises(McpEvidenceError):
            proven_mcp_probe(result, nonce)
        items = [row["item"] for row in result["items"] if row["item"].get("type") == "mcpToolCall"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "failed")
        self.assertIsNone(items[0]["result"])
        self.assertIsNone(self.fixture.synthetic_consent)
        requests = [
            event
            for event in self.fixture.events[event_offset:]
            if event.get("method") == "mcpServer/elicitation/request"
        ]
        self.assertEqual(len(requests), 1)
        self.assertTrue(
            any(
                event.get("fixture_event") == "mcp_consent_decision"
                and event.get("request_id") == requests[0]["id"]
                and event.get("action") == "decline"
                for event in self.fixture.events[event_offset:]
            )
        )


if __name__ == "__main__":
    unittest.main()
