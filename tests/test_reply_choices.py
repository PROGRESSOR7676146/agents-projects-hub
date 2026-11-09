from __future__ import annotations

import unittest

from hermes_codex_router.reply_choices import (
    CHOICE_PART_MARKER,
    choice_part_html,
    extract_reply_choices,
    parse_choice_part,
)


class ReplyChoicesTests(unittest.TestCase):
    def test_trailing_marker_is_removed_and_labels_are_returned(self) -> None:
        text, labels = extract_reply_choices("Pick one.\n\n[[choices: Yes | No | Later]]\n")
        self.assertEqual(text, "Pick one.")
        self.assertEqual(labels, ("Yes", "No", "Later"))

    def test_marker_is_case_insensitive_and_tolerates_spacing(self) -> None:
        text, labels = extract_reply_choices("Q?\n[[ Choices :  A  |B ]]")
        self.assertEqual((text, labels), ("Q?", ("A", "B")))

    def test_text_without_marker_is_unchanged(self) -> None:
        self.assertEqual(extract_reply_choices("Plain answer"), ("Plain answer", ()))

    def test_marker_not_on_the_last_line_is_ignored(self) -> None:
        source = "[[choices: A | B]]\nMore text after it"
        self.assertEqual(extract_reply_choices(source), (source, ()))

    def test_marker_inside_an_open_code_fence_is_ignored(self) -> None:
        source = "Example:\n```\n[[choices: A | B]]"
        self.assertEqual(extract_reply_choices(source), (source, ()))

    def test_invalid_label_sets_leave_the_text_visible(self) -> None:
        for marker in (
            "[[choices: Only]]",
            "[[choices: A | A]]",
            "[[choices: A | ]]",
            "[[choices: " + " | ".join(str(i) for i in range(7)) + "]]",
            "[[choices: A | " + "x" * 41 + "]]",
        ):
            source = f"Question\n{marker}"
            self.assertEqual(extract_reply_choices(source), (source, ()), marker)

    def test_marker_alone_is_not_removed_into_an_empty_answer(self) -> None:
        source = "[[choices: A | B]]"
        self.assertEqual(extract_reply_choices(source), ("", ("A", "B")))

    def test_choice_part_round_trips_escaped_labels(self) -> None:
        labels = ("A & B", "<tag>", "Нет")
        part = choice_part_html(labels)
        self.assertTrue(part.startswith(CHOICE_PART_MARKER))
        self.assertNotIn("<tag>", part)
        self.assertEqual(parse_choice_part(part), labels)

    def test_ordinary_html_is_not_a_choice_part(self) -> None:
        self.assertIsNone(parse_choice_part("Hello"))
        self.assertIsNone(parse_choice_part(CHOICE_PART_MARKER + "\nOnly"))


if __name__ == "__main__":
    unittest.main()
