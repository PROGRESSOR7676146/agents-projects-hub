from __future__ import annotations

import json
import unittest
import uuid

from hermes_codex_router.claude_corrections import (
    ClaudeCorrectionCallbacks,
    ClaudeCorrectionInput,
    ClaudeInputFrame,
)
from hermes_codex_router.claude_host_profile import host_event_policy
from hermes_codex_router.claude_stream import (
    ClaudeStreamError,
    ClaudeStreamReader,
    parse_claude_stream,
)

SESSION = "00000000-0000-4000-8000-000000000001"


class ClaudeCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.child = ClaudeInputFrame(str(uuid.uuid4()), SESSION, "Example correction")
        self.events: list[tuple[object, ...]] = []
        self.channel = ClaudeCorrectionInput(
            "Example initial",
            SESSION,
            ClaudeCorrectionCallbacks(
                prepare=lambda frame: self.events.append(("prepare", frame.uuid)),
                claim=lambda: self.child,
                receipt=lambda identifier, boundary: self.events.append(
                    ("receipt", identifier, boundary)
                ),
                result=lambda boundary: self.events.append(("result", boundary)),
            ),
        )
        self.reader = ClaudeStreamReader(expected_session_id=SESSION, correction_input=self.channel)

    def feed(self, event: dict[str, object]) -> None:
        self.reader.feed((json.dumps(event) + "\n").encode())

    def echo(self, frame: ClaudeInputFrame) -> dict[str, object]:
        return {**json.loads(frame.encode()), "isReplay": True}

    def result(self, text: str) -> dict[str, object]:
        return dict(
            type="result", subtype="success", is_error=False, session_id=SESSION, result=text
        )

    def test_receipt_before_result_completes_one_native_turn(self) -> None:
        self.feed(self.echo(self.channel.initial))
        self.assertEqual(self.channel.poll(), self.child.encode())
        self.feed(self.echo(self.child))
        self.feed(self.result("Combined answer"))
        self.assertTrue(self.channel.complete)
        self.assertIsNone(self.channel.poll())
        parsed = parse_claude_stream(self.reader.finish(), expected_session_id=SESSION)
        self.assertEqual(parsed.text, "Combined answer")
        self.assertIn(("receipt", self.child.uuid, 1), self.events)

    def test_late_input_waits_for_its_own_result_without_new_admission(self) -> None:
        self.feed(self.echo(self.channel.initial))
        self.channel.poll()
        self.feed(self.result("First answer"))
        self.assertFalse(self.reader.terminal_seen)
        self.assertIsNone(self.channel.poll())
        self.feed(self.echo(self.child))
        self.feed(self.result("Correction answer"))
        self.assertTrue(self.reader.terminal_seen)
        parsed = parse_claude_stream(self.reader.finish(), expected_session_id=SESSION)
        self.assertIn("First answer", parsed.text)
        self.assertIn("Correction answer", parsed.text)
        self.assertIn(("receipt", self.child.uuid, 2), self.events)

    def test_write_and_echo_do_not_prove_completion(self) -> None:
        self.feed(self.echo(self.channel.initial))
        self.channel.poll()
        self.feed(self.echo(self.child))
        with self.assertRaises(ClaudeStreamError):
            self.reader.finish()

    def test_missing_conflicting_duplicate_or_foreign_receipt_refuses(self) -> None:
        for mutation in ("uuid", "text", "replay", "session", "duplicate"):
            with self.subTest(mutation=mutation):
                self.setUp()
                event = self.echo(self.channel.initial)
                if mutation == "uuid":
                    event["uuid"] = str(uuid.uuid4())
                elif mutation == "text":
                    event["message"]["content"] = "changed"  # type: ignore[index]
                elif mutation == "replay":
                    event["isReplay"] = False
                elif mutation == "session":
                    event["session_id"] = str(uuid.uuid4())
                else:
                    self.feed(event)
                with self.assertRaises(ClaudeStreamError):
                    self.feed(event)

    def host_reader(self) -> ClaudeStreamReader:
        self.reader = ClaudeStreamReader(
            expected_session_id=SESSION,
            correction_input=self.channel,
            event_policy=host_event_policy,
        )
        return self.reader

    def subagent(self, content: object, **extra: object) -> dict[str, object]:
        return {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": "toolu_01ExampleAgentCall",
            "session_id": SESSION,
            "uuid": str(uuid.uuid4()),
            **extra,
        }

    def test_host_subagent_messages_are_not_input_receipts(self) -> None:
        # A host turn that delegates to a native subagent receives the child's
        # prompt as a user event owned by the Agent call. It must neither end
        # the turn as unverified nor count as the submission's receipt.
        self.host_reader()
        self.feed(self.subagent([{"type": "text", "text": "Example child prompt"}], isReplay=True))
        self.assertEqual([item for item in self.events if item[0] == "receipt"], [])
        self.feed(self.echo(self.channel.initial))
        self.feed(self.subagent("Example child prompt"))
        self.feed(self.subagent([{"type": "text", "text": "Example child prompt"}]))
        self.feed(
            self.subagent([{"type": "tool_result", "tool_use_id": "toolu_02", "content": "x"}])
        )
        self.feed(self.result("Answer after delegation"))
        self.assertTrue(self.channel.complete)
        parsed = parse_claude_stream(self.reader.finish(), expected_session_id=SESSION)
        self.assertEqual(parsed.text, "Answer after delegation")
        self.assertEqual(
            [item for item in self.events if item[0] == "receipt"],
            [("receipt", self.channel.initial.uuid, 1)],
        )

    def test_subagent_events_never_complete_or_acknowledge_the_submission(self) -> None:
        # Child-only replay followed by a result: no receipt of the submission.
        self.host_reader()
        self.feed(
            self.subagent(json.loads(self.channel.initial.encode())["message"], isReplay=True)
        )
        with self.assertRaises(ClaudeStreamError):
            self.feed(self.result("Unowned answer"))
        # A parent-owned replay of the correction cannot acknowledge it.
        self.setUp()
        self.host_reader()
        self.feed(self.echo(self.channel.initial))
        self.channel.poll()
        self.feed({**self.echo(self.child), "parent_tool_use_id": "toolu_01ExampleAgentCall"})
        self.feed(self.result("First answer"))
        with self.assertRaises(ClaudeStreamError):
            self.reader.finish()
        # A child-owned terminal result cannot complete the host turn.
        self.setUp()
        self.host_reader()
        self.feed(self.echo(self.channel.initial))
        with self.assertRaisesRegex(ClaudeStreamError, "subagent result"):
            self.feed({**self.result("Child answer"), "parent_tool_use_id": "toolu_01Example"})

    def test_restrictive_policies_reject_subagent_prompts_first(self) -> None:
        from hermes_codex_router.claude_file_policy import file_tool_event_policy

        for policy in (None, file_tool_event_policy("editor")):
            with self.subTest(policy=policy):
                self.setUp()
                self.reader = ClaudeStreamReader(
                    expected_session_id=SESSION, correction_input=self.channel, event_policy=policy
                )
                self.feed(self.echo(self.channel.initial))
                with self.assertRaisesRegex(ClaudeStreamError, "policy"):
                    self.feed(self.subagent("Example child prompt"))

    def test_unowned_or_malformed_user_text_still_refuses(self) -> None:
        for parent in (None, "", "   ", 7):
            with self.subTest(parent=parent):
                self.setUp()
                self.host_reader()
                self.feed(self.echo(self.channel.initial))
                event = self.subagent([{"type": "text", "text": "Injected"}])
                event["parent_tool_use_id"] = parent
                with self.assertRaises(ClaudeStreamError):
                    self.feed(event)

    def test_parent_result_with_unacknowledged_child_is_not_completion(self) -> None:
        self.feed(self.echo(self.channel.initial))
        self.channel.poll()
        self.feed(self.result("First answer"))
        with self.assertRaises(ClaudeStreamError):
            self.reader.finish()

    def test_two_results_share_one_aggregate_character_bound(self) -> None:
        for size, accepted in ((99_998, True), (100_000, False)):
            with self.subTest(size=size):
                self.setUp()
                self.feed(self.echo(self.channel.initial))
                self.channel.poll()
                self.feed(self.result("a" * 100_000))
                self.feed(self.echo(self.child))
                if accepted:
                    self.feed(self.result("b" * size))
                    self.assertEqual(len(parse_claude_stream(self.reader.finish()).text), 200_000)
                else:
                    with self.assertRaisesRegex(ClaudeStreamError, "aggregate result"):
                        self.feed(self.result("b" * size))

    def test_persistence_failure_aborts_without_a_second_claim(self) -> None:
        self.channel.callbacks.claim = lambda: (_ for _ in ()).throw(RuntimeError("example fault"))
        with self.assertRaises(ClaudeStreamError):
            self.channel.poll()


if __name__ == "__main__":
    unittest.main()
