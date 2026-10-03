from __future__ import annotations

import copy
import unittest
from dataclasses import FrozenInstanceError, asdict
from typing import Any

from hermes_codex_router.codex_activity import (
    CodexActivityCategory,
    CodexActivityEvent,
    CodexActivityKind,
    normalize_codex_activity,
    normalize_codex_approval_resolution,
)


class CodexActivityTests(unittest.TestCase):
    def message(self, method: str, **params: Any) -> dict[str, Any]:
        return {
            "method": method,
            "params": {"threadId": "thread-example", "turnId": "turn-example", **params},
        }

    def normalize(self, message: object) -> CodexActivityEvent | None:
        return normalize_codex_activity(
            message, expected_thread_id="thread-example", expected_turn_id="turn-example"
        )

    def test_known_tool_lifecycles_preserve_identity_and_fixed_category(self) -> None:
        tools: tuple[tuple[str, CodexActivityCategory], ...] = (
            ("commandExecution", "command"),
            ("fileChange", "file_change"),
            ("mcpToolCall", "mcp"),
            ("dynamicToolCall", "dynamic_tool"),
            ("collabAgentToolCall", "collaboration"),
            ("webSearch", "web_search"),
            ("imageView", "image_view"),
        )
        lifecycles: tuple[tuple[str, str, CodexActivityKind], ...] = (
            ("item/started", "inProgress", "tool_started"),
            ("item/completed", "completed", "tool_completed"),
        )
        for item_type, category in tools:
            for method, status, kind in lifecycles:
                with self.subTest(item_type=item_type, method=method):
                    item: dict[str, Any] = {"id": "item-example", "type": item_type}
                    if item_type not in {"webSearch", "imageView"}:
                        item["status"] = status
                    result = self.normalize(self.message(method, item=item))
                    self.assertEqual(
                        result,
                        CodexActivityEvent(
                            kind, category, "thread-example", "turn-example", "item-example"
                        ),
                    )

    def test_completed_declined_or_failed_tool_is_an_activity_edge_not_turn_proof(self) -> None:
        for item_type, statuses in (
            ("commandExecution", ("failed", "declined")),
            ("fileChange", ("failed", "declined")),
            ("mcpToolCall", ("failed",)),
            ("dynamicToolCall", ("failed",)),
            ("collabAgentToolCall", ("failed",)),
        ):
            for status in statuses:
                with self.subTest(item_type=item_type, status=status):
                    event = self.normalize(
                        self.message(
                            "item/completed",
                            item={"id": "item-example", "type": item_type, "status": status},
                        )
                    )
                    assert event is not None
                    self.assertEqual(event.kind, "tool_completed")
                    self.assertNotIn("terminal", asdict(event))

    def test_tool_status_must_match_lifecycle_and_documented_category(self) -> None:
        for method, item in (
            ("item/started", {"id": "item-example", "type": "commandExecution"}),
            ("item/started", {"id": "item-example", "type": "fileChange", "status": "completed"}),
            (
                "item/completed",
                {"id": "item-example", "type": "commandExecution", "status": "inProgress"},
            ),
            ("item/completed", {"id": "item-example", "type": "mcpToolCall", "status": "declined"}),
            ("item/completed", {"id": "item-example", "type": "dynamicToolCall", "status": True}),
            ("item/completed", {"id": "item-example", "type": "imageView", "status": "unknown"}),
        ):
            with self.subTest(method=method, item=item):
                self.assertIsNone(self.normalize(self.message(method, item=item)))

    def test_nonempty_command_output_records_activity_without_output(self) -> None:
        event = self.normalize(
            self.message(
                "item/commandExecution/outputDelta",
                itemId="item-example",
                delta="fictional sensitive command output /home/example/private.txt",
            )
        )
        self.assertEqual(
            event,
            CodexActivityEvent(
                "tool_output", "command", "thread-example", "turn-example", "item-example"
            ),
        )
        for delta in (None, "", " \n\t", 1, {}, []):
            with self.subTest(delta=delta):
                self.assertIsNone(
                    self.normalize(
                        self.message(
                            "item/commandExecution/outputDelta",
                            itemId="item-example",
                            delta=delta,
                        )
                    )
                )

    def test_visible_completed_message_records_only_identity_and_safe_phase(self) -> None:
        for phase in ("commentary", "final_answer", None):
            with self.subTest(phase=phase):
                item = {"id": "item-example", "type": "agentMessage", "text": "Visible progress"}
                if phase is not None:
                    item["phase"] = phase
                event = self.normalize(self.message("item/completed", item=item))
                self.assertEqual(
                    event,
                    CodexActivityEvent(
                        "visible_message_completed",
                        "visible_message",
                        "thread-example",
                        "turn-example",
                        "item-example",
                        phase=phase or "unknown",
                    ),
                )
        event = self.normalize(
            self.message(
                "item/completed",
                item={
                    "id": "item-example",
                    "type": "agentMessage",
                    "text": "Visible progress",
                    "phase": None,
                },
            )
        )
        assert event is not None
        self.assertEqual(event.phase, "unknown")

    def test_unrecognized_message_phase_and_empty_text_do_not_count_as_progress(self) -> None:
        for change in (
            {"phase": "analysis"},
            {"phase": "reasoning"},
            {"phase": "unknown"},
            {"phase": ""},
            {"phase": False},
            {"text": ""},
            {"text": " \n"},
            {"text": None},
        ):
            with self.subTest(change=change):
                self.assertIsNone(
                    self.normalize(
                        self.message(
                            "item/completed",
                            item={
                                "id": "item-example",
                                "type": "agentMessage",
                                "text": "Visible progress",
                                **change,
                            },
                        )
                    )
                )
        self.assertIsNone(
            self.normalize(
                self.message(
                    "item/started",
                    item={
                        "id": "item-example",
                        "type": "agentMessage",
                        "text": "Visible progress",
                    },
                )
            )
        )

    def test_retrying_error_has_no_error_payload_or_execution_authority(self) -> None:
        event = self.normalize(
            self.message(
                "error",
                willRetry=True,
                error={"message": "fictional sensitive failure"},
            )
        )
        self.assertEqual(
            event, CodexActivityEvent("retrying", "retry", "thread-example", "turn-example")
        )
        for change in (
            {"willRetry": False},
            {"willRetry": None},
            {"willRetry": 1},
            {"willRetry": "true"},
            {"error": None},
            {"error": "raw failure"},
        ):
            with self.subTest(change=change):
                self.assertIsNone(
                    self.normalize(
                        self.message(
                            "error",
                            **{
                                "willRetry": True,
                                "error": {"message": "fictional failure"},
                                **change,
                            },
                        )
                    )
                )

    def test_approval_requests_preserve_request_id_type_and_fixed_permission_category(self) -> None:
        requests: tuple[tuple[str, CodexActivityCategory, dict[str, Any]], ...] = (
            ("item/commandExecution/requestApproval", "command", {}),
            ("item/commandExecution/requestApproval", "command", {"networkApprovalContext": None}),
            (
                "item/commandExecution/requestApproval",
                "network",
                {
                    "networkApprovalContext": {"host": "example.com", "protocol": "https"},
                },
            ),
            ("item/fileChange/requestApproval", "file_change", {}),
            ("item/permissions/requestApproval", "permissions", {"permissions": {"network": {}}}),
        )
        for method, category, extra in requests:
            for request_id in (0, 81, -1, "request-example", "81"):
                with self.subTest(method=method, request_id=request_id, extra=extra):
                    message = self.message(method, itemId="item-example", **extra)
                    message["id"] = request_id
                    self.assertEqual(
                        self.normalize(message),
                        CodexActivityEvent(
                            "approval_requested",
                            category,
                            "thread-example",
                            "turn-example",
                            "item-example",
                            request_id=request_id,
                        ),
                    )

    def test_approval_requires_valid_item_and_request_id(self) -> None:
        for request_id in (
            None,
            True,
            False,
            1.5,
            "",
            "has spaces",
            "/home/example",
            "x" * 257,
            2**63,
            -(2**63) - 1,
            {},
            [],
        ):
            with self.subTest(request_id=request_id):
                message = self.message("item/fileChange/requestApproval", itemId="item-example")
                message["id"] = request_id
                self.assertIsNone(self.normalize(message))
        self.assertIsNone(
            self.normalize(self.message("item/fileChange/requestApproval", itemId="item-example"))
        )
        message = self.message("item/fileChange/requestApproval")
        message["id"] = 81
        self.assertIsNone(self.normalize(message))

    def test_approval_payload_shape_does_not_invent_a_permission_category(self) -> None:
        for method, extra in (
            ("item/permissions/requestApproval", {}),
            ("item/permissions/requestApproval", {"permissions": None}),
            ("item/commandExecution/requestApproval", {"networkApprovalContext": []}),
            ("item/commandExecution/requestApproval", {"networkApprovalContext": {}}),
            (
                "item/commandExecution/requestApproval",
                {"networkApprovalContext": {"host": "example.com", "protocol": 5}},
            ),
        ):
            with self.subTest(method=method, extra=extra):
                message = self.message(method, itemId="item-example", **extra)
                message["id"] = 81
                self.assertIsNone(self.normalize(message))

    def test_no_returned_field_contains_commands_paths_output_reasoning_or_errors(self) -> None:
        sentinel = "fictional-sensitive-payload"
        messages = [
            self.message(
                "item/started",
                item={
                    "id": "item-example",
                    "type": "commandExecution",
                    "status": "inProgress",
                    "command": sentinel,
                    "cwd": "/home/example",
                    "reasoning": sentinel,
                },
            ),
            self.message(
                "item/completed",
                item={
                    "id": "item-example",
                    "type": "mcpToolCall",
                    "status": "failed",
                    "server": sentinel,
                    "tool": sentinel,
                    "arguments": {"path": sentinel},
                    "result": sentinel,
                    "error": sentinel,
                },
            ),
            self.message(
                "item/completed",
                item={
                    "id": "item-example",
                    "type": "agentMessage",
                    "text": sentinel,
                },
            ),
            self.message("error", willRetry=True, error={"message": sentinel}),
        ]
        approval = self.message(
            "item/fileChange/requestApproval",
            itemId="item-example",
            reason=sentinel,
            grantRoot="/home/example",
        )
        approval["id"] = 81
        messages.append(approval)
        for message in messages:
            with self.subTest(method=message["method"]):
                event = self.normalize(message)
                assert event is not None
                self.assertNotIn(sentinel, repr(event))
                self.assertNotIn("/home/example", repr(event))
                self.assertEqual(
                    set(asdict(event)),
                    {
                        "kind",
                        "category",
                        "thread_id",
                        "turn_id",
                        "item_id",
                        "request_id",
                        "phase",
                    },
                )

    def test_missing_malformed_and_mismatched_scope_cannot_be_inferred(self) -> None:
        for key in ("threadId", "turnId"):
            for value in (None, True, 1, "", "different", "bad id", "/home/example", "x" * 257):
                with self.subTest(key=key, value=value):
                    message = self.message(
                        "item/commandExecution/outputDelta", itemId="item-example", delta="output"
                    )
                    message["params"][key] = value
                    self.assertIsNone(self.normalize(message))
            message = self.message(
                "item/commandExecution/outputDelta", itemId="item-example", delta="output"
            )
            del message["params"][key]
            self.assertIsNone(self.normalize(message))

    def test_expected_identity_is_required_valid_and_bounded(self) -> None:
        message = self.message(
            "item/commandExecution/outputDelta", itemId="item-example", delta="output"
        )
        for key in ("expected_thread_id", "expected_turn_id"):
            for value in (None, True, "", "bad id", "x" * 257):
                with self.subTest(key=key, value=value):
                    scope: dict[str, Any] = {
                        "expected_thread_id": "thread-example",
                        "expected_turn_id": "turn-example",
                    }
                    scope[key] = value
                    self.assertIsNone(normalize_codex_activity(message, **scope))
        message["params"]["threadId"] = "x" * 256
        message["params"]["turnId"] = "y" * 256
        message["params"]["itemId"] = "z" * 256
        self.assertIsNotNone(
            normalize_codex_activity(
                message,
                expected_thread_id="x" * 256,
                expected_turn_id="y" * 256,
            )
        )

    def test_item_identity_is_not_truncated_or_repaired(self) -> None:
        for item_id in (None, True, 1, "", "has spaces", "line\nbreak", "/home/example", "x" * 257):
            with self.subTest(item_id=item_id):
                self.assertIsNone(
                    self.normalize(
                        self.message(
                            "item/commandExecution/outputDelta",
                            itemId=item_id,
                            delta="output",
                        )
                    )
                )
                self.assertIsNone(
                    self.normalize(
                        self.message(
                            "item/started",
                            item={
                                "id": item_id,
                                "type": "commandExecution",
                                "status": "inProgress",
                            },
                        )
                    )
                )

    def test_conflicting_redundant_item_identity_is_rejected(self) -> None:
        item = {"id": "item-example", "type": "commandExecution", "status": "inProgress"}
        self.assertIsNone(
            self.normalize(self.message("item/started", item=item, itemId="different"))
        )
        self.assertIsNone(self.normalize(self.message("item/started", item=item, itemId=None)))
        self.assertIsNotNone(
            self.normalize(self.message("item/started", item=item, itemId="item-example"))
        )

    def test_unknown_event_types_and_private_thinking_never_produce_activity(self) -> None:
        for method in (
            "item/agentMessage/delta",
            "item/reasoning/textDelta",
            "item/reasoning/summaryTextDelta",
            "item/fileChange/outputDelta",
            "thread/tokenUsage/updated",
            "account/rateLimits/updated",
            "turn/started",
            "turn/completed",
            "turn/plan/updated",
            "serverRequest/resolved",
            "item/tool/requestUserInput",
            "unknown/event",
        ):
            with self.subTest(method=method):
                self.assertIsNone(
                    self.normalize(self.message(method, itemId="item-example", delta="text"))
                )
        for item_type in (
            "reasoning",
            "plan",
            "contextCompaction",
            "userMessage",
            "futureTool",
            None,
            True,
            {},
        ):
            with self.subTest(item_type=item_type):
                self.assertIsNone(
                    self.normalize(
                        self.message(
                            "item/completed",
                            item={
                                "id": "item-example",
                                "type": item_type,
                                "status": "completed",
                                "text": "thinking",
                            },
                        )
                    )
                )

    def test_malformed_envelopes_and_request_notification_confusion_are_rejected(self) -> None:
        for message in (
            None,
            [],
            "text",
            3,
            {},
            {"method": []},
            {"method": "item/started", "params": []},
        ):
            with self.subTest(message=message):
                self.assertIsNone(self.normalize(message))
        for extra in ({"id": None}, {"id": 81}, {"result": {}}, {"error": {}}, {"jsonrpc": "1.0"}):
            with self.subTest(extra=extra):
                self.assertIsNone(
                    self.normalize(
                        {
                            **self.message(
                                "item/commandExecution/outputDelta",
                                itemId="item-example",
                                delta="output",
                            ),
                            **extra,
                        }
                    )
                )

    def test_normalization_is_pure_and_event_is_immutable(self) -> None:
        message = self.message(
            "item/commandExecution/outputDelta", itemId="item-example", delta="output"
        )
        before = copy.deepcopy(message)
        event = self.normalize(message)
        assert event is not None
        self.assertEqual(message, before)
        self.assertEqual(self.normalize(message), event)
        with self.assertRaises(FrozenInstanceError):
            setattr(event, "item_id", "changed")

    def test_resolution_requires_prior_exact_approval_and_safe_notification_shape(self) -> None:
        requested = CodexActivityEvent(
            "approval_requested", "command", "thread-example", "turn-example", "item-example", 71
        )
        notification: dict[str, Any] = {
            "method": "serverRequest/resolved",
            "params": {"threadId": "thread-example", "requestId": 71},
        }
        result = normalize_codex_approval_resolution(notification, requested=requested)
        self.assertEqual(
            result,
            CodexActivityEvent(
                "approval_resolved", "command", "thread-example", "turn-example", "item-example", 71
            ),
        )
        variants: list[dict[str, Any]] = [
            {**notification, "id": 17},
            {**notification, "result": {}},
            {**notification, "error": {}},
            {**notification, "jsonrpc": "1.0"},
            {**notification, "method": "turn/completed"},
        ]
        for value in (True, "71", [], {}, None):
            variants.append(
                {**notification, "params": {"threadId": "thread-example", "requestId": value}}
            )
        for message in variants:
            with self.subTest(message=message):
                self.assertIsNone(normalize_codex_approval_resolution(message, requested=requested))
        assert result is not None
        self.assertIsNone(normalize_codex_approval_resolution(notification, requested=result))


if __name__ == "__main__":
    unittest.main()
