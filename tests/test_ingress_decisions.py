from __future__ import annotations

import unittest

from hermes_codex_router.ingress_decisions import (
    ControlCommandDecision,
    EmergencyStopDecision,
    IgnoreDecision,
    IngressDecisionContext,
    PassiveForwardDecision,
    ProductiveRouteDecision,
    decide_ingress,
)
from hermes_codex_router.telegram import IncomingAttachment, TopicMessage


class IngressDecisionTests(unittest.TestCase):
    usernames = {
        "codex": "example_codex_bot",
        "gemini": "example_gemini_bot",
        "opencode": "example_opencode_bot",
    }

    def context(
        self,
        *,
        active_agent_id: str = "codex",
        pending_batch_agent_id: str | None = None,
        queue_enabled_agent_ids: frozenset[str] = frozenset({"codex", "gemini"}),
        managed_external_agent_ids: frozenset[str] = frozenset({"opencode"}),
        primary_agent_id: str = "codex",
    ) -> IngressDecisionContext:
        return IngressDecisionContext(
            active_agent_id=active_agent_id,
            pending_batch_agent_id=pending_batch_agent_id,
            usernames=self.usernames,
            hub_username="example_hub_bot",
            managed_external_agent_ids=managed_external_agent_ids,
            queue_enabled_agent_ids=queue_enabled_agent_ids,
            primary_agent_id=primary_agent_id,
        )

    @staticmethod
    def message(
        text: str,
        *,
        reply_to_username: str | None = None,
        is_forwarded: bool = False,
        attachments: tuple[IncomingAttachment, ...] = (),
        unavailable_materials: tuple[str, ...] = (),
        message_id: int = 1,
    ) -> TopicMessage:
        return TopicMessage(
            update_id=message_id,
            message_id=message_id,
            chat_id=-1001234567890,
            thread_id=77,
            chat_title="Example",
            sender_id=42,
            text=text,
            reply_to_username=reply_to_username,
            is_forwarded=is_forwarded,
            attachments=attachments,
            unavailable_materials=unavailable_materials,
        )

    def test_reply_wins_over_mentions(self) -> None:
        decision = decide_ingress(
            self.message(
                "@example_codex_bot ask the other provider",
                reply_to_username="example_gemini_bot",
            ),
            self.context(),
        )

        assert isinstance(decision, ProductiveRouteDecision)
        self.assertEqual(decision.targets, ("gemini",))
        self.assertEqual(decision.admission, "accept")
        self.assertEqual(decision.prompt_text, "@example_codex_bot ask the other provider")

    def test_mentions_preserve_text_order_and_dedupe(self) -> None:
        decision = decide_ingress(
            self.message(
                "@example_gemini_bot first @example_codex_bot second @example_gemini_bot again"
            ),
            self.context(),
        )

        assert isinstance(decision, ProductiveRouteDecision)
        self.assertEqual(decision.targets, ("gemini", "codex"))
        self.assertEqual(decision.admission, "reject_multiple_queue_targets")

    def test_unknown_mentions_fall_back_to_active_agent(self) -> None:
        decision = decide_ingress(self.message("ask @someone_else to check this"), self.context())

        assert isinstance(decision, ProductiveRouteDecision)
        self.assertEqual(decision.targets, ("codex",))
        self.assertEqual(decision.admission, "accept")

    def test_hub_mention_is_removed_before_provider_routing(self) -> None:
        decision = decide_ingress(
            self.message("@example_hub_bot @example_codex_bot inspect this"),
            self.context(),
        )

        assert isinstance(decision, ProductiveRouteDecision)
        self.assertEqual(decision.targets, ("codex",))
        self.assertEqual(decision.routing_text, "@example_codex_bot inspect this")
        self.assertEqual(decision.prompt_text, "inspect this")

    def test_pending_batch_target_is_inherited_only_by_unaddressed_queue_input(self) -> None:
        inherited = decide_ingress(
            self.message("second part"),
            self.context(pending_batch_agent_id="gemini"),
        )
        explicitly_addressed = decide_ingress(
            self.message("@example_codex_bot second part"),
            self.context(pending_batch_agent_id="gemini"),
        )
        replied = decide_ingress(
            self.message("second part", reply_to_username="example_codex_bot"),
            self.context(pending_batch_agent_id="gemini"),
        )

        assert isinstance(inherited, ProductiveRouteDecision)
        self.assertEqual(inherited.targets, ("gemini",))
        self.assertEqual(inherited.admission, "accept")
        self.assertEqual(inherited.prompt_text, "second part")
        assert isinstance(explicitly_addressed, ProductiveRouteDecision)
        self.assertEqual(explicitly_addressed.targets, ("codex",))
        self.assertEqual(explicitly_addressed.prompt_text, "second part")
        assert isinstance(replied, ProductiveRouteDecision)
        self.assertEqual(replied.targets, ("codex",))
        self.assertEqual(replied.prompt_text, "second part")

        not_queue_enabled = decide_ingress(
            self.message("third part"),
            self.context(
                pending_batch_agent_id="gemini",
                queue_enabled_agent_ids=frozenset({"codex"}),
            ),
        )
        assert isinstance(not_queue_enabled, ProductiveRouteDecision)
        self.assertEqual(not_queue_enabled.targets, ("codex",))

    def test_forward_is_passive_before_stop_or_command(self) -> None:
        for text in ("/stop", "/status"):
            with self.subTest(text=text):
                decision = decide_ingress(
                    self.message(text, is_forwarded=True),
                    self.context(),
                )
                assert isinstance(decision, PassiveForwardDecision)
                self.assertEqual(decision.targets, ())

    def test_stop_is_an_emergency_decision_when_not_forwarded(self) -> None:
        decision = decide_ingress(self.message("/STOP@example_hub_bot"), self.context())

        assert isinstance(decision, EmergencyStopDecision)
        self.assertEqual(decision.admission, "accept")

    def test_command_with_material_is_rejected_as_control_command(self) -> None:
        attachment = IncomingAttachment(
            kind="document",
            file_id="file-example",
            file_unique_id="unique-example",
            file_name="example.txt",
            mime_type="text/plain",
            file_size=12,
        )
        decision = decide_ingress(
            self.message("/status", attachments=(attachment,)),
            self.context(),
        )

        assert isinstance(decision, ControlCommandDecision)
        self.assertEqual(decision.command.name, "status")
        self.assertEqual(decision.admission, "reject_material")

    def test_external_only_target_is_ignored_by_controller(self) -> None:
        decision = decide_ingress(
            self.message("@example_opencode_bot"),
            self.context(),
        )

        assert isinstance(decision, IgnoreDecision)
        self.assertEqual(decision.reason, "managed_external_only")

    def test_unaddressed_external_active_route_is_pending_batch_eligible(self) -> None:
        decision = decide_ingress(
            self.message("second part"),
            self.context(active_agent_id="opencode"),
        )

        assert isinstance(decision, IgnoreDecision)
        self.assertTrue(decision.pending_batch_eligible)

    def test_pending_local_batch_overrides_active_managed_external_agent(self) -> None:
        decision = decide_ingress(
            self.message("second part"),
            self.context(active_agent_id="opencode", pending_batch_agent_id="gemini"),
        )

        assert isinstance(decision, ProductiveRouteDecision)
        self.assertEqual(decision.targets, ("gemini",))

    def test_context_and_unknown_commands_keep_productive_command_metadata(self) -> None:
        context = decide_ingress(self.message("/context antigravity 8"), self.context())
        unknown = decide_ingress(self.message("/unknown request"), self.context())

        assert isinstance(context, ProductiveRouteDecision)
        self.assertIsNotNone(context.parsed_command)
        assert context.parsed_command is not None
        self.assertEqual(context.parsed_command.name, "context")
        assert isinstance(unknown, ProductiveRouteDecision)
        self.assertIsNotNone(unknown.parsed_command)
        assert unknown.parsed_command is not None
        self.assertEqual(unknown.parsed_command.name, "unknown")

    def test_any_command_with_material_is_rejected_as_control_command(self) -> None:
        attachment = IncomingAttachment(
            kind="document",
            file_id="file-example",
            file_unique_id="unique-example",
            file_name="example.txt",
            mime_type="text/plain",
            file_size=12,
        )
        for text in ("/context", "/unknown request"):
            with self.subTest(text=text):
                decision = decide_ingress(
                    self.message(text, attachments=(attachment,)), self.context()
                )
                assert isinstance(decision, ControlCommandDecision)
                self.assertEqual(decision.admission, "reject_material")

    def test_empty_local_provider_mention_is_rejected_without_admission(self) -> None:
        decision = decide_ingress(
            self.message("@example_codex_bot"),
            self.context(),
        )

        assert isinstance(decision, ProductiveRouteDecision)
        self.assertEqual(decision.targets, ("codex",))
        self.assertEqual(decision.admission, "reject_empty_request")
        self.assertEqual(decision.prompt_text, "")

    def test_multiple_queue_targets_are_rejected(self) -> None:
        decision = decide_ingress(
            self.message("@example_codex_bot and @example_gemini_bot do this"),
            self.context(),
        )

        assert isinstance(decision, ProductiveRouteDecision)
        self.assertEqual(decision.targets, ("codex", "gemini"))
        self.assertEqual(decision.admission, "reject_multiple_queue_targets")

    def test_multiple_targets_are_not_rejected_when_primary_agent_is_inline(self) -> None:
        reversed_order = decide_ingress(
            self.message("@example_codex_bot and @example_gemini_bot do this"),
            self.context(
                primary_agent_id="codex",
                queue_enabled_agent_ids=frozenset({"gemini"}),
            ),
        )

        assert isinstance(reversed_order, ProductiveRouteDecision)
        self.assertEqual(reversed_order.targets, ("codex", "gemini"))
        self.assertEqual(reversed_order.admission, "accept")
        self.assertTrue(reversed_order.requires_inline_root)
        self.assertEqual(reversed_order.prompt_target_id, "codex")
        self.assertEqual(reversed_order.prompt_text, "and @example_gemini_bot do this")

        primary_last = decide_ingress(
            self.message("@example_gemini_bot @example_codex_bot do this"),
            self.context(
                primary_agent_id="codex",
                queue_enabled_agent_ids=frozenset({"gemini"}),
            ),
        )

        assert isinstance(primary_last, ProductiveRouteDecision)
        self.assertEqual(primary_last.targets, ("gemini", "codex"))
        self.assertEqual(primary_last.admission, "accept")
        self.assertEqual(primary_last.prompt_target_id, "codex")
        self.assertEqual(primary_last.prompt_text, "@example_gemini_bot  do this")

    def test_mixed_local_and_managed_external_targets_keep_only_local_admission(self) -> None:
        decision = decide_ingress(
            self.message("@example_opencode_bot and @example_codex_bot do this"),
            self.context(),
        )

        assert isinstance(decision, ProductiveRouteDecision)
        self.assertEqual(decision.targets, ("opencode", "codex"))
        self.assertEqual(decision.local_targets, ("codex",))
        self.assertEqual(decision.external_targets, ("opencode",))
        self.assertEqual(decision.admission, "accept")
        self.assertFalse(decision.requires_inline_root)

    def test_inline_local_target_requires_root_and_rejects_material(self) -> None:
        attachment = IncomingAttachment(
            kind="document",
            file_id="file-example",
            file_unique_id="unique-example",
            file_name="example.txt",
            mime_type="text/plain",
            file_size=12,
        )
        decision = decide_ingress(
            self.message("@example_gemini_bot inspect this", attachments=(attachment,)),
            self.context(queue_enabled_agent_ids=frozenset({"codex"})),
        )

        assert isinstance(decision, ProductiveRouteDecision)
        self.assertEqual(decision.local_targets, ("gemini",))
        self.assertTrue(decision.requires_inline_root)
        self.assertEqual(decision.admission, "reject_material_inline")

    def test_material_only_message_uses_productive_fallback_text(self) -> None:
        attachment = IncomingAttachment(
            kind="document",
            file_id="file-example",
            file_unique_id="unique-example",
            file_name="example.txt",
            mime_type="text/plain",
            file_size=12,
        )
        decision = decide_ingress(
            self.message("", attachments=(attachment,)),
            self.context(),
        )

        assert isinstance(decision, ProductiveRouteDecision)
        self.assertEqual(decision.targets, ("codex",))
        self.assertEqual(decision.admission, "accept")
        self.assertEqual(decision.routing_text, "")
        self.assertEqual(decision.prompt_text, "Review the attached Telegram material.")


if __name__ == "__main__":
    unittest.main()
