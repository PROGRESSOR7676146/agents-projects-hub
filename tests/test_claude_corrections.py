from __future__ import annotations

import json
import unittest
import uuid

from hermes_codex_router.claude_corrections import (
    ClaudeCorrectionCallbacks,
    ClaudeCorrectionInput,
    ClaudeInputFrame,
)
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

    def test_parent_result_with_unacknowledged_child_is_not_completion(self) -> None:
        self.feed(self.echo(self.channel.initial))
        self.channel.poll()
        self.feed(self.result("First answer"))
        with self.assertRaises(ClaudeStreamError):
            self.reader.finish()

    def test_persistence_failure_aborts_without_a_second_claim(self) -> None:
        self.channel.callbacks.claim = lambda: (_ for _ in ()).throw(RuntimeError("example fault"))
        with self.assertRaises(ClaudeStreamError):
            self.channel.poll()


if __name__ == "__main__":
    unittest.main()
