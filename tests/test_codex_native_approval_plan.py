"""Fixed offline request budgets and exact deny/resolution sequencing."""

from __future__ import annotations

import io
import unittest
from http.client import HTTPMessage
from unittest.mock import Mock, patch

from tests import codex_native_profile_actor as actor

TOOLS = [
    {
        "type": "function",
        "name": "exec_command",
        "parameters": {
            "properties": {
                "cmd": {"type": "string"},
                "sandbox_permissions": {"type": "string"},
                "justification": {"type": "string"},
            }
        },
    }
]


class NativeApprovalPlanTests(unittest.TestCase):
    def data(self, number):
        return {
            "tools": TOOLS,
            "input": [
                {
                    "type": "function_call_output",
                    "call_id": f"call_example-initial_{number - 1}",
                    "output": "Command rejected by user",
                }
            ],
        }

    def test_fixed_mode_limits_preserve_existing_cap(self):
        self.assertEqual(actor.approval_budget("approval_compatibility"), (2, 4))
        self.assertEqual(actor.approval_budget("approval_sequence"), (129, 130))
        with self.assertRaises(ValueError):
            actor.approval_budget("example-untrusted-limit")

    def test_complete_sequence_requires_exact_denied_output_and_primary_resolution(self):
        for kind, calls in (("approval_compatibility", 2), ("approval_sequence", 129)):
            plan = actor.ResponsePlan(kind=kind, requests=calls + 1, resolved=calls)
            self.assertEqual(actor.approval_output(plan, self.data(calls + 1))["type"], "message")
            plan.resolved -= 1
            with self.assertRaises(ValueError):
                actor.approval_output(plan, self.data(calls + 1))

    def test_over_limit_duplicate_output_and_non_denial_refuse(self):
        for kind, cap in (("approval_compatibility", 4), ("approval_sequence", 130)):
            with self.assertRaises(ValueError):
                actor.approval_output(actor.ResponsePlan(kind=kind, requests=cap + 1), self.data(2))
        plan = actor.ResponsePlan(kind="approval_sequence", requests=2, resolved=1)
        for data in (
            {"tools": TOOLS, "input": self.data(2)["input"] * 2},
            {
                "tools": TOOLS,
                "input": [
                    {
                        "type": "function_call_output",
                        "call_id": "call_example-initial_1",
                        "output": "success",
                    }
                ],
            },
            self.data(3),
        ):
            with self.assertRaises(ValueError):
                actor.approval_output(plan, data)

    def test_one_fixed_command_never_requests_prefix_or_permission_grants(self):
        output = actor.approval_output(
            actor.ResponsePlan(kind="approval_sequence", requests=1), {"tools": TOOLS}
        )
        self.assertEqual(output["type"], "function_call")
        import json

        args = json.loads(output["arguments"])
        self.assertEqual(args["cmd"], "/usr/bin/true")
        self.assertEqual(args["sandbox_permissions"], "require_escalated")
        self.assertNotIn("prefix_rule", args)

    def test_rejected_post_attempts_consume_fixed_case_and_global_budget(self):
        for kind in ("command", "notifications", "approval_compatibility", "approval_sequence"):
            with self.subTest(kind=kind):
                plan = actor.ResponsePlan(kind=kind)
                with (
                    patch.object(actor, "plan", plan),
                    patch.object(actor, "total_requests", 0),
                    patch.object(actor, "emit"),
                ):
                    for number, (path, length, body) in enumerate(
                        (
                            ("/example-unexpected", "2", b"{}"),
                            ("/v1/responses", "0", b""),
                            ("/v1/responses", "bad", b""),
                            ("/v1/responses", "2000001", b""),
                            ("/v1/responses", "1", b"{"),
                            ("/v1/responses", "2", b"[]"),
                        ),
                        start=1,
                    ):
                        handler = object.__new__(actor.Handler)
                        handler.path = path
                        handler.headers = HTTPMessage()
                        handler.headers["Content-Length"] = length
                        handler.rfile = io.BytesIO(body)
                        handler.send_error = Mock()
                        handler.send_response = Mock()
                        actor.Handler.do_POST(handler)
                        self.assertEqual(plan.requests, number)
                        self.assertEqual(actor.total_requests, number)
                        handler.send_error.assert_called_once()
                        handler.send_response.assert_not_called()
