from __future__ import annotations

import hashlib
import html
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from hermes_codex_router.topic_panel_view import (
    MAX_PANEL_CHARACTERS,
    MAX_PANEL_LINES,
    SILENCE_HIGHLIGHT_SECONDS,
    ActivityKind,
    Headline,
    PanelSnapshot,
    panel_body_digest,
    render_panel_body,
    render_panel_message,
)

OBSERVED = datetime(2026, 11, 20, 11, 32, tzinfo=UTC)
SILENCE = "Новых наблюдаемых событий нет 10+ мин. Это не означает остановку."


class RenderPanelBodyTests(unittest.TestCase):
    def test_every_headline(self) -> None:
        cases: tuple[tuple[Headline, str], ...] = (
            ("needs_owner", "🔴 Нужно решение · Агент"),
            ("stop_requested", "🟠 Остановка запрошена · Агент"),
            ("local_writer", "🟡 Локальный терминал"),
            ("executing", "🟢 Агент"),
            ("preparing", "🟢 Запуск · Агент"),
            ("queued", "⏳ В очереди · Агент"),
            ("idle", "⚪ Свободно"),
        )
        for headline, expected in cases:
            with self.subTest(headline=headline):
                snapshot = PanelSnapshot(OBSERVED, headline, agent="Агент")
                self.assertEqual(render_panel_body(snapshot), expected)

    def test_typical_running_turn_states_each_fact_once(self) -> None:
        started = OBSERVED - timedelta(seconds=30)
        snapshot = PanelSnapshot(
            OBSERVED,
            "executing",
            agent="Claude",
            model="claude-opus-5-5",
            started_at=started,
            activity_kind="message",
            activity_at=started,
            interim_delivered_at=started,
        )
        body = render_panel_body(snapshot)
        # The event keeps its time even in the start minute (independent review).
        self.assertEqual(
            body, "🟢 Claude opus-5-5 · с 14:31\nСообщение агента, промежуточная доставка 14:31"
        )
        self.assertEqual(body.count("14:31"), 2)  # The start and the event, each once.
        self.assertEqual(body.casefold().count("claude"), 1)

    def test_owner_reasons_are_ordered_deduplicated_and_owner_only(self) -> None:
        snapshot = PanelSnapshot(
            OBSERVED,
            "needs_owner",
            owner_reasons=("approval", "uncertain", "delivery_unknown", "approval"),
        )
        self.assertEqual(
            render_panel_body(snapshot).splitlines(),
            [
                "🔴 Нужно решение",
                "Агент ждёт разрешения; исход неизвестен, решение в уведомлении выше; "
                "доставка не подтверждена, тема ждёт решения",
            ],
        )
        self.assertEqual(render_panel_body(replace(snapshot, headline="idle")), "⚪ Свободно")

    def test_approval_wait_is_stated_once_with_its_time(self) -> None:
        snapshot = PanelSnapshot(
            OBSERVED,
            "needs_owner",
            owner_reasons=("approval",),
            activity_kind="approval",
            activity_at=OBSERVED,
        )
        self.assertEqual(
            render_panel_body(snapshot), "🔴 Нужно решение\nАгент ждёт разрешения с 14:32"
        )
        # Without the approval reason the event keeps its own label.
        other = replace(snapshot, owner_reasons=("uncertain",))
        self.assertIn("Запрос разрешения 14:32", render_panel_body(other))

    def test_model_rules(self) -> None:
        headlines: tuple[Headline, ...] = (
            "needs_owner",
            "stop_requested",
            "local_writer",
            "executing",
            "preparing",
            "queued",
            "idle",
        )
        for headline in headlines:
            with self.subTest(headline=headline):
                body = render_panel_body(PanelSnapshot(OBSERVED, headline, model="модель"))
                expected = headline not in ("idle", "local_writer")
                self.assertEqual("модель" in body, expected)
        self.assertEqual(render_panel_body(PanelSnapshot(OBSERVED, "executing", model="")), "🟢")
        # A model that does not repeat the agent stays whole.
        codex = PanelSnapshot(OBSERVED, "executing", agent="Codex", model="gpt-6")
        self.assertEqual(render_panel_body(codex), "🟢 Codex gpt-6")
        # Only a whole agent-name prefix is dropped.
        prefix = PanelSnapshot(OBSERVED, "executing", agent="Clau", model="claude-x")
        self.assertEqual(render_panel_body(prefix), "🟢 Clau claude-x")

    def test_activity_labels_and_headline_rules(self) -> None:
        cases: tuple[tuple[ActivityKind, str], ...] = (
            ("tool", "Инструмент"),
            ("message", "Сообщение агента"),
            ("approval", "Запрос разрешения"),
        )
        for kind, label in cases:
            for headline in ("executing", "needs_owner", "stop_requested"):
                with self.subTest(kind=kind, headline=headline):
                    snapshot = PanelSnapshot(
                        OBSERVED,
                        headline,
                        activity_kind=kind,
                        activity_at=OBSERVED,
                    )
                    self.assertIn(f"{label} 14:32", render_panel_body(snapshot).splitlines())
        snapshot = PanelSnapshot(OBSERVED, "idle", activity_kind="tool", activity_at=OBSERVED)
        self.assertEqual(render_panel_body(snapshot), "⚪ Свободно")

    def test_activity_without_kind_has_generic_label(self) -> None:
        snapshot = PanelSnapshot(OBSERVED, "executing", activity_at=OBSERVED)
        self.assertIn("Событие 14:32", render_panel_body(snapshot).splitlines())

    def test_distinct_event_times_stay_separate(self) -> None:
        snapshot = PanelSnapshot(
            OBSERVED,
            "executing",
            started_at=OBSERVED - timedelta(minutes=5),
            activity_kind="tool",
            activity_at=OBSERVED - timedelta(minutes=1),
            interim_delivered_at=OBSERVED - timedelta(minutes=3),
        )
        self.assertEqual(
            render_panel_body(snapshot).splitlines()[1],
            "Инструмент 14:31 · промежуточная доставка 14:29",
        )

    def test_silence_threshold_and_no_events_yet(self) -> None:
        started = OBSERVED - timedelta(seconds=SILENCE_HIGHLIGHT_SECONDS)
        snapshot = PanelSnapshot(OBSERVED, "executing", started_at=started)
        body = render_panel_body(snapshot)
        self.assertEqual(body, "🟢 · с 14:22\n" + SILENCE)
        self.assertNotIn("Событий пока нет", body)

        just_below = replace(snapshot, observed_at=OBSERVED - timedelta(seconds=1))
        body = render_panel_body(just_below)
        self.assertNotIn(SILENCE, body)
        self.assertIn("Событий пока нет", body.splitlines())

    def test_silence_uses_latest_evidence(self) -> None:
        snapshot = PanelSnapshot(
            OBSERVED,
            "executing",
            started_at=OBSERVED - timedelta(hours=1),
            activity_kind="tool",
            activity_at=OBSERVED - timedelta(minutes=30),
            interim_delivered_at=OBSERVED - timedelta(minutes=10),
        )
        body = render_panel_body(snapshot)
        self.assertIn(SILENCE, body.splitlines())
        self.assertIn("промежуточная доставка 14:22", body)
        recent = replace(snapshot, interim_delivered_at=OBSERVED - timedelta(minutes=9))
        self.assertNotIn(SILENCE, render_panel_body(recent))
        self.assertNotIn("Событий пока нет", render_panel_body(recent))
        self.assertNotIn("доставка", render_panel_body(replace(snapshot, headline="needs_owner")))

    def test_queue_with_and_without_held(self) -> None:
        snapshot = PanelSnapshot(OBSERVED, "queued", agent="Агент", queued=3)
        self.assertEqual(render_panel_body(snapshot), "⏳ В очереди: 3 · Агент")
        self.assertEqual(
            render_panel_body(replace(snapshot, held=2)), "⏳ В очереди: 3, удержано 2 · Агент"
        )
        self.assertEqual(
            render_panel_body(replace(snapshot, queued=0, held=2)),
            "⏳ В очереди: 0, удержано 2 · Агент",
        )
        self.assertEqual(render_panel_body(replace(snapshot, queued=0)), "⏳ В очереди · Агент")
        # Behind a running turn the counts join the details line.
        running = replace(snapshot, headline="executing", held=1)
        self.assertIn("В очереди 3, удержано 1", render_panel_body(running).splitlines())

    def test_idle_results(self) -> None:
        snapshot = PanelSnapshot(OBSERVED, "idle", last_result="delivered")
        self.assertEqual(render_panel_body(snapshot), "⚪ Свободно · ответ отправлен")
        self.assertEqual(
            render_panel_body(replace(snapshot, last_result_at=OBSERVED)),
            "⚪ Свободно · ответ отправлен 14:32",
        )
        saved = replace(snapshot, last_result="saved", last_result_at=OBSERVED)
        self.assertEqual(
            render_panel_body(saved), "⚪ Свободно · ответ сохранён, доставка не подтверждена"
        )
        self.assertNotIn("ответ", render_panel_body(replace(saved, headline="queued")))

    def test_html_escaping(self) -> None:
        snapshot = PanelSnapshot(OBSERVED, "executing", agent='<b>&"', model='<b>&"')
        self.assertEqual(render_panel_body(snapshot), '🟢 &lt;b&gt;&amp;" &lt;b&gt;&amp;"')

    def test_cross_day_and_explicit_timezone(self) -> None:
        snapshot = PanelSnapshot(
            OBSERVED,
            "idle",
            started_at=datetime(2026, 11, 19, 20, 59, tzinfo=UTC),
            last_result="delivered",
            last_result_at=datetime(2026, 11, 19, 21, 1, tzinfo=UTC),
        )
        self.assertEqual(
            render_panel_body(snapshot), "⚪ Свободно · с 19.11 23:59 · ответ отправлен 00:01"
        )
        self.assertEqual(
            render_panel_body(snapshot, timezone=ZoneInfo("UTC")),
            "⚪ Свободно · с 19.11 20:59 · ответ отправлен 19.11 21:01",
        )

    def test_all_naive_timestamps_raise_even_when_not_rendered(self) -> None:
        naive = datetime(2026, 11, 20, 11, 32)
        snapshot = PanelSnapshot(OBSERVED, "idle")
        cases = (
            replace(snapshot, observed_at=naive),
            replace(snapshot, started_at=naive),
            replace(snapshot, activity_at=naive),
            replace(snapshot, interim_delivered_at=naive),
            replace(snapshot, last_result_at=naive),
        )
        for case in cases:
            with self.subTest(snapshot=case), self.assertRaises(ValueError):
                render_panel_body(case)

    def test_bounds_preserve_headline_and_collapse_identity(self) -> None:
        snapshot = PanelSnapshot(
            OBSERVED,
            "needs_owner",
            owner_reasons=("uncertain", "delivery_unknown", "approval"),
            model="<b>&" * MAX_PANEL_CHARACTERS,
            started_at=OBSERVED,
            queued=2,
        )
        body = render_panel_body(snapshot)
        self.assertTrue(body)
        self.assertLessEqual(len(body.splitlines()), MAX_PANEL_LINES)
        self.assertLessEqual(len(body), MAX_PANEL_CHARACTERS)
        # Identity text is collapsed to 80 characters before escaping.
        self.assertEqual(
            body.splitlines()[0],
            "🔴 Нужно решение · " + html.escape("<b>&" * 20, quote=False) + " · с 14:32",
        )

        huge_agent = PanelSnapshot(OBSERVED, "executing", agent="<" * MAX_PANEL_CHARACTERS)
        self.assertEqual(render_panel_body(huge_agent), "🟢 " + "&lt;" * 80)

    def test_both_identity_fields_at_maximum_with_footer_stay_bounded(self) -> None:
        snapshot = PanelSnapshot(
            OBSERVED,
            "executing",
            agent="<&" * MAX_PANEL_CHARACTERS,
            model="\"'>" * MAX_PANEL_CHARACTERS,
            started_at=OBSERVED - timedelta(minutes=20),
            activity_kind="tool",
            activity_at=OBSERVED - timedelta(minutes=15),
            interim_delivered_at=OBSERVED - timedelta(minutes=12),
            queued=99,
            held=99,
        )
        body = render_panel_body(snapshot)
        self.assertLessEqual(len(body), MAX_PANEL_CHARACTERS)
        message = render_panel_message(body, prepared_at=OBSERVED)
        self.assertLessEqual(len(message), MAX_PANEL_CHARACTERS + 40)
        self.assertLessEqual(len(message), 4096)
        self.assertNotIn("<&", message)

    def test_multiline_identity_stays_one_line(self) -> None:
        snapshot = PanelSnapshot(
            OBSERVED,
            "executing",
            agent="Агент\n" * 11,
            model="модель\n\nвторая",
            started_at=OBSERVED,
            queued=1,
            held=1,
        )
        lines = render_panel_body(snapshot).splitlines()
        self.assertEqual(lines[0], "🟢 " + " ".join(["Агент"] * 11) + " модель вторая · с 14:32")
        self.assertLessEqual(len(lines), MAX_PANEL_LINES)

    def test_digest_stability_without_threshold_crossing(self) -> None:
        snapshot = PanelSnapshot(OBSERVED, "executing", started_at=OBSERVED - timedelta(minutes=2))
        body = render_panel_body(snapshot)
        self.assertEqual(panel_body_digest(body), hashlib.sha256(body.encode("utf-8")).hexdigest())
        self.assertEqual(panel_body_digest(body), panel_body_digest(render_panel_body(snapshot)))
        later = replace(snapshot, observed_at=OBSERVED + timedelta(minutes=1))
        self.assertEqual(body, render_panel_body(later))
        self.assertEqual(panel_body_digest(body), panel_body_digest(render_panel_body(later)))

    def test_message_footer_is_one_short_line(self) -> None:
        message = render_panel_message("⚪ Свободно", prepared_at=OBSERVED)
        self.assertEqual(message, "⚪ Свободно\n<i>изменено 20.11 14:32</i>")


if __name__ == "__main__":
    unittest.main()
