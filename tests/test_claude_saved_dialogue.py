"""Synthetic saved-dialogue gates; no native CLI, accounts or inference."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.claude_saved_dialogue import ExpectedDialogue, inspect_saved_dialogue

SESSION = "00000000-0000-4000-8000-000000000001"
ROOT = "/workspace/example"
MESSAGES = (("user", "Example request."), ("assistant", "example-answer"))


def node(number: int, kind: str, parent: int | None, text: str = "") -> dict:
    def identity(n: int) -> str:
        return f"00000000-0000-4000-8000-{n:012d}"

    record = {
        "type": kind,
        "sessionId": SESSION,
        "cwd": ROOT,
        "isSidechain": False,
        "version": "2.1.285",
        "uuid": identity(number),
        "parentUuid": None if parent is None else identity(parent),
    }
    if kind in {"user", "assistant"}:
        record["message"] = {
            "role": kind,
            "content": text if kind == "user" else [{"type": "text", "text": text}],
        }
    else:
        record["attachment"] = {"type": "example", "text": "Example metadata."}
    return record


class SavedDialogueTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="example-saved-dialogue-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / (SESSION + ".jsonl")
        self.expected = ExpectedDialogue(SESSION, ROOT, MESSAGES)
        self.records = [
            node(10, "user", None, MESSAGES[0][1]),
            node(11, "assistant", 10, MESSAGES[1][1]),
        ]

    def write(self, records: list[dict] | None = None, suffix: bytes = b"") -> None:
        data = b"".join(
            json.dumps(record, separators=(",", ":")).encode() + b"\n"
            for record in (self.records if records is None else records)
        )
        self.path.write_bytes(data + suffix)

    def status(self, expected: ExpectedDialogue | None = None, path: str | None = None):
        return inspect_saved_dialogue(
            str(self.path) if path is None else path, expected or self.expected
        )

    def test_exact_dialogue_is_ready_and_result_contains_no_transcript(self) -> None:
        self.write()
        result = self.status()
        self.assertEqual(result, "ready")
        for secret in (SESSION, ROOT, str(self.path), MESSAGES[0][1], MESSAGES[1][1]):
            self.assertNotIn(secret, repr(result))
            self.assertNotIn(secret, repr(self.expected))

    def test_missing_file_or_parent_and_empty_file_wait(self) -> None:
        self.assertEqual(self.status(), "waiting")
        self.assertEqual(
            self.status(path=str(self.directory / "missing" / self.path.name)), "waiting"
        )
        self.path.touch()
        self.assertEqual(self.status(), "waiting")

    def test_exact_prefix_waits_until_final_assistant_append(self) -> None:
        self.write(self.records[:1])
        self.assertEqual(self.status(), "waiting")
        with self.path.open("ab") as output:
            output.write(json.dumps(self.records[1]).encode() + b"\n")
        self.assertEqual(self.status(), "ready")

    def test_fragment_and_complete_json_without_newline_still_wait(self) -> None:
        for suffix in (b'{"type":', json.dumps(self.records[1]).encode(), b'"\xe2\x82'):
            with self.subTest(suffix_length=len(suffix)):
                self.write(self.records[:1], suffix)
                self.assertEqual(self.status(), "waiting")

    def test_partial_suffix_prevents_ready_even_after_expected_pair(self) -> None:
        self.write(suffix=b'{"type":')
        self.assertEqual(self.status(), "waiting")

    def test_malformed_completed_record_is_invalid_despite_partial_suffix(self) -> None:
        for prefix in (b"{broken}\n", b"\xff\n", b"[]\n", b"\n"):
            self.path.write_bytes(prefix + b'{"unfinished":')
            self.assertEqual(self.status(), "invalid")

    def test_attachments_participate_in_single_chain_and_metadata_does_not(self) -> None:
        records = [
            {"type": "queue-operation", "sessionId": SESSION, "operation": "enqueue"},
            node(10, "user", None, MESSAGES[0][1]),
            node(11, "attachment", 10),
            {"type": "atis-latch", "sessionId": SESSION, "atis": "example"},
            node(12, "assistant", 11, MESSAGES[1][1]),
            {
                "type": "last-prompt",
                "sessionId": SESSION,
                "leafUuid": node(12, "attachment", 11)["uuid"],
            },
            {"type": "cost-state", "sessionId": SESSION, "totalDuration": 1},
        ]
        self.write(records)
        self.assertEqual(self.status(), "ready")

    def test_six_message_three_phase_expectation_accepts_exact_prefix_only(self) -> None:
        messages = MESSAGES + (("user", "Example second."), ("assistant", "example-second"))
        messages += (("user", "Example third."), ("assistant", "example-third"))
        expected = ExpectedDialogue(SESSION, ROOT, messages)
        records = [
            node(10 + i, role, None if i == 0 else 9 + i, text)
            for i, (role, text) in enumerate(messages)
        ]
        self.write(records[:-1])
        self.assertEqual(self.status(expected), "waiting")
        self.write(records)
        self.assertEqual(self.status(expected), "ready")

    def test_wrong_uuid_root_version_and_falsey_sidechain_are_invalid(self) -> None:
        for key, value in (
            ("sessionId", node(99, "user", None)["uuid"]),
            ("cwd", "/workspace/other-example"),
            ("version", "2.1.284"),
            ("isSidechain", True),
            ("isSidechain", 0),
            ("isSidechain", None),
            ("uuid", "not-a-uuid"),
        ):
            records = copy.deepcopy(self.records)
            records[1][key] = value
            self.write(records)
            with self.subTest(field=key, value_type=type(value).__name__):
                self.assertEqual(self.status(), "invalid")

    def test_missing_required_chain_field_is_invalid(self) -> None:
        for key in ("sessionId", "cwd", "version", "isSidechain", "uuid", "parentUuid", "message"):
            records = copy.deepcopy(self.records)
            del records[1][key]
            self.write(records)
            self.assertEqual(self.status(), "invalid")

    def test_branches_cycles_forward_parents_duplicate_and_noncanonical_uuid_refuse(self) -> None:
        variants = [
            [node(10, "user", 11, MESSAGES[0][1]), node(11, "assistant", 10, MESSAGES[1][1])],
            [
                node(10, "user", None, MESSAGES[0][1]),
                node(11, "attachment", 10),
                node(12, "assistant", 10, MESSAGES[1][1]),
            ],
            [node(10, "user", None, MESSAGES[0][1]), node(10, "assistant", 10, MESSAGES[1][1])],
        ]
        noncanonical = copy.deepcopy(self.records)
        noncanonical[0]["uuid"] = "00000000000040008000000000000010"
        variants.append(noncanonical)
        for records in variants:
            self.write(records)
            self.assertEqual(self.status(), "invalid")

    def test_extra_reordered_or_wrong_completed_reply_is_invalid(self) -> None:
        variants = [
            self.records + [node(12, "user", 11, "Example unexpected request.")],
            [node(10, "assistant", None, MESSAGES[1][1]), node(11, "user", 10, MESSAGES[0][1])],
            [self.records[0], node(11, "assistant", 10, "example-wrong-answer")],
        ]
        for records in variants:
            self.write(records)
            self.assertEqual(self.status(), "invalid")

    def test_role_and_text_block_shape_must_match_without_concatenation(self) -> None:
        variants = [
            {"role": "user", "content": MESSAGES[1][1]},
            {"role": "assistant", "content": MESSAGES[1][1]},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "example-"},
                    {"type": "text", "text": "answer"},
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "fictional-hidden"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": MESSAGES[1][1], "extra": True}],
            },
        ]
        for message in variants:
            records = copy.deepcopy(self.records)
            records[1]["message"] = message
            self.write(records)
            self.assertEqual(self.status(), "invalid")

    def test_unknown_types_and_disguised_metadata_dialogue_are_invalid(self) -> None:
        for extra in (
            {"type": "unknown", "sessionId": SESSION},
            {"type": "cost-state", "sessionId": SESSION, "message": None},
            {
                "type": "queue-operation",
                "sessionId": SESSION,
                "uuid": node(12, "user", None)["uuid"],
            },
            {"type": "atis-latch", "sessionId": SESSION, "parentUuid": None},
        ):
            self.write(self.records + [extra])
            self.assertEqual(self.status(), "invalid")

    def test_metadata_identity_checks_apply_even_without_dialogue(self) -> None:
        for extra in (
            {"type": "cost-state"},
            {"type": "cost-state", "sessionId": SESSION, "cwd": "/workspace/other-example"},
            {"type": "cost-state", "sessionId": SESSION, "isSidechain": 0},
        ):
            self.write(self.records + [extra])
            self.assertEqual(self.status(), "invalid")

    def test_attachment_cannot_hide_top_level_message(self) -> None:
        attachment = node(12, "attachment", 11)
        attachment["message"] = {"role": "assistant", "content": "example-extra"}
        self.write(self.records + [attachment])
        self.assertEqual(self.status(), "invalid")

    def test_duplicate_keys_nonfinite_depth_nodes_and_surrogates_refuse(self) -> None:
        for raw in (
            b'{"type":"cost-state","type":"user"}',
            b'{"type":"cost-state","x":NaN}',
            b'{"type":"cost-state","x":1e999}',
            b"[" * 13 + b"0" + b"]" * 13,
            json.dumps(
                {"type": "cost-state", "sessionId": SESSION, "x": list(range(257))}
            ).encode(),
            b'{"type":"cost-state","x":"\\ud800"}',
        ):
            self.path.write_bytes(raw + b"\n")
            self.assertEqual(self.status(), "invalid")

    def test_line_fragment_and_record_count_bounds_are_enforced(self) -> None:
        metadata = json.dumps({"type": "cost-state", "sessionId": SESSION}).encode() + b"\n"
        for raw in (b"x" * 16385, b"x" * 16385 + b"\n", metadata * 513):
            self.path.write_bytes(raw)
            self.assertEqual(self.status(), "invalid")

    def test_record_count_and_fragment_at_limit_are_accepted(self) -> None:
        metadata = {"type": "cost-state", "sessionId": SESSION}
        self.write(self.records + [metadata] * 510)
        self.assertEqual(self.status(), "ready")
        self.write(suffix=b"x" * 16384)
        self.assertEqual(self.status(), "waiting")

    def test_short_reads_reconstruct_the_complete_snapshot(self) -> None:
        self.write()
        actual_read = os.read

        def short_read(descriptor, size):
            return actual_read(descriptor, min(size, 7))

        with patch("tests.claude_saved_dialogue.os.read", side_effect=short_read):
            self.assertEqual(self.status(), "ready")

    def test_growth_during_read_is_bounded_and_waits(self) -> None:
        self.write()
        actual_read = os.read
        sizes = []
        changed = False

        def growing(descriptor, size):
            nonlocal changed
            if not changed:
                with self.path.open("ab") as output:
                    output.write(b"x" * (1024 * 1024))
                changed = True
            result = actual_read(descriptor, size)
            sizes.append(len(result))
            return result

        with patch("tests.claude_saved_dialogue.os.read", side_effect=growing):
            self.assertEqual(self.status(), "waiting")
        self.assertEqual(sum(sizes), 1024 * 1024 + 1)

    def test_oversized_regular_file_is_rejected_before_read(self) -> None:
        with self.path.open("wb") as output:
            output.truncate(1024 * 1024 + 1)
        with patch("tests.claude_saved_dialogue.os.read") as read:
            self.assertEqual(self.status(), "invalid")
            read.assert_not_called()

    def test_symlink_fifo_directory_and_hardlink_are_refused_without_read(self) -> None:
        target = self.directory / "example-target"
        target.write_text("example-only")
        for kind in ("symlink", "fifo", "directory", "hardlink"):
            if kind == "symlink":
                self.path.symlink_to(target)
            elif kind == "fifo":
                os.mkfifo(self.path)
            elif kind == "directory":
                self.path.mkdir()
            else:
                os.link(target, self.path)
            with patch("tests.claude_saved_dialogue.os.read") as read:
                self.assertEqual(self.status(), "invalid")
                read.assert_not_called()
            if kind == "directory":
                self.path.rmdir()
            else:
                self.path.unlink()

    def test_symlink_ancestor_is_not_followed(self) -> None:
        self.write()
        linked = self.directory / "example-link"
        linked.symlink_to(self.directory, target_is_directory=True)
        with patch("tests.claude_saved_dialogue.os.read") as read:
            self.assertEqual(self.status(path=str(linked / self.path.name)), "invalid")
            read.assert_not_called()

    def test_invalid_expectation_or_path_refuses_before_open(self) -> None:
        variants = [
            ExpectedDialogue("not-a-uuid", ROOT, MESSAGES),
            ExpectedDialogue(SESSION, "relative/example", MESSAGES),
            ExpectedDialogue(SESSION, ROOT, (("assistant", "example"),)),
            ExpectedDialogue(SESSION, ROOT, MESSAGES * 4),
            ExpectedDialogue(SESSION, ROOT, (("user", "\ud800"), ("assistant", "example"))),
        ]
        paths = [
            "relative/example.jsonl",
            str(self.directory / "other.jsonl"),
            str(self.directory) + "/../" + self.path.name,
            str(self.directory) + "/./" + self.path.name,
            str(self.path) + "\0",
            "/" + "/".join(["example"] * 33) + "/" + self.path.name,
            str(self.directory) + "//" + self.path.name,
            str(self.path) + "/",
            "/\ud800/" + self.path.name,
        ]
        with patch("tests.claude_saved_dialogue.os.open") as opened:
            for expected in variants:
                self.assertEqual(self.status(expected), "invalid")
            for path in paths:
                self.assertEqual(self.status(path=path), "invalid")
            opened.assert_not_called()

    def test_file_replacement_after_read_is_waiting_not_ready(self) -> None:
        self.write()
        from tests.claude_saved_dialogue import _strict_json

        replaced = False

        def replacing(raw):
            nonlocal replaced
            if not replaced:
                replacement = self.directory / "example-replacement"
                replacement.write_bytes(self.path.read_bytes())
                replacement.replace(self.path)
                replaced = True
            return _strict_json(raw)

        with patch("tests.claude_saved_dialogue._strict_json", side_effect=replacing):
            self.assertEqual(self.status(), "waiting")
        self.assertTrue(replaced)

    def test_append_during_parse_is_waiting_even_when_read_pair_was_exact(self) -> None:
        self.write()
        from tests.claude_saved_dialogue import _strict_json

        changed = False

        def appending(raw):
            nonlocal changed
            if not changed:
                with self.path.open("ab") as output:
                    output.write(b'{"next":')
                changed = True
            return _strict_json(raw)

        with patch("tests.claude_saved_dialogue._strict_json", side_effect=appending):
            self.assertEqual(self.status(), "waiting")
        self.assertTrue(changed)

    def test_file_becoming_symlink_during_parse_waits_without_following(self) -> None:
        self.write()
        from tests.claude_saved_dialogue import _strict_json

        changed = False

        def replacing(raw):
            nonlocal changed
            if not changed:
                target = self.directory / "example-old-file"
                self.path.rename(target)
                self.path.symlink_to(target)
                changed = True
            return _strict_json(raw)

        with patch("tests.claude_saved_dialogue._strict_json", side_effect=replacing):
            self.assertEqual(self.status(), "waiting")
        self.assertTrue(changed)

    def test_ancestor_replacement_during_parse_is_waiting(self) -> None:
        original = self.directory / "example-store"
        original.mkdir()
        self.path = original / self.path.name
        self.write()
        from tests.claude_saved_dialogue import _strict_json

        changed = False

        def replacing(raw):
            nonlocal changed
            if not changed:
                original.rename(self.directory / "example-old-store")
                original.mkdir()
                self.write()
                changed = True
            return _strict_json(raw)

        with patch("tests.claude_saved_dialogue._strict_json", side_effect=replacing):
            self.assertEqual(self.status(), "waiting")
        self.assertTrue(changed)

    def test_read_and_open_faults_do_not_expose_exception_text_or_leak_fds(self) -> None:
        self.write()
        before = len(tuple(Path("/proc/self/fd").iterdir()))
        for function in ("os.read", "os.open"):
            with patch(
                "tests.claude_saved_dialogue." + function, side_effect=OSError("fictional-secret")
            ):
                self.assertEqual(self.status(), "invalid")
        self.assertEqual(len(tuple(Path("/proc/self/fd").iterdir())), before)

    def test_repeated_ready_waiting_invalid_leave_no_owned_descriptors(self) -> None:
        before = len(tuple(Path("/proc/self/fd").iterdir()))
        for _ in range(3):
            self.write()
            self.assertEqual(self.status(), "ready")
            self.write(self.records[:1])
            self.assertEqual(self.status(), "waiting")
            self.path.write_bytes(b"invalid\n")
            self.assertEqual(self.status(), "invalid")
        self.assertEqual(len(tuple(Path("/proc/self/fd").iterdir())), before)
