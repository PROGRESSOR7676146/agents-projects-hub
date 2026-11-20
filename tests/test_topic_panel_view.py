from __future__ import annotations

import hashlib
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
)

OBSERVED = datetime(2026, 11, 20, 11, 32, tzinfo=UTC)


class RenderPanelBodyTests(unittest.TestCase):
    def test_every_headline(self) -> None:
        cases: tuple[tuple[Headline, str], ...] = (
            ("needs_owner", "🔴 Нужно ваше решение"),
            ("stop_requested", "🟠 Остановка запрошена"),
            ("local_writer", "🟡 Управление в локальном терминале"),
            ("executing", "🟢 Выполняется · Агент"),
            ("preparing", "🟢 Подготовка запуска · Агент"),
            ("queued", "⏳ В очереди"),
            ("idle", "⚪ Нет активных задач"),
        )
        for headline, expected in cases:
            with self.subTest(headline=headline):
                snapshot = PanelSnapshot(OBSERVED, headline, agent="Агент")
                self.assertEqual(render_panel_body(snapshot), expected)

    def test_owner_reasons_are_ordered_deduplicated_and_owner_only(self) -> None:
        snapshot = PanelSnapshot(
            OBSERVED,
            "needs_owner",
            owner_reasons=("approval", "uncertain", "delivery_unknown", "approval"),
        )
        self.assertEqual(
            render_panel_body(snapshot).splitlines(),
            [
                "🔴 Нужно ваше решение",
                "Агент ждёт разрешения",
                "Исход задачи неизвестен — решение в уведомлении выше",
                "Доставка ответа не подтверждена — тема ждёт решения",
            ],
        )
        self.assertEqual(
            render_panel_body(replace(snapshot, headline="idle")),
            "⚪ Нет активных задач",
        )

    def test_model_line_rules(self) -> None:
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
                expected = headline in (
                    "needs_owner",
                    "stop_requested",
                    "executing",
                    "preparing",
                )
                self.assertEqual("Модель: модель" in body, expected)
        self.assertNotIn(
            "Модель:", render_panel_body(PanelSnapshot(OBSERVED, "executing", model=""))
        )

    def test_activity_labels_and_headline_rules(self) -> None:
        cases: tuple[tuple[ActivityKind, str], ...] = (
            ("tool", "работа инструмента"),
            ("message", "сообщение агента"),
            ("approval", "ожидание разрешения"),
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
                    self.assertIn(
                        f"Последнее событие: {label} · 14:32",
                        render_panel_body(snapshot),
                    )
        snapshot = PanelSnapshot(OBSERVED, "idle", activity_kind="tool", activity_at=OBSERVED)
        self.assertNotIn("Последнее событие:", render_panel_body(snapshot))

    def test_activity_without_kind_has_generic_label(self) -> None:
        snapshot = PanelSnapshot(OBSERVED, "executing", activity_at=OBSERVED)
        self.assertIn("Последнее событие · 14:32", render_panel_body(snapshot))

    def test_silence_threshold_and_no_events_yet(self) -> None:
        started = OBSERVED - timedelta(seconds=SILENCE_HIGHLIGHT_SECONDS)
        snapshot = PanelSnapshot(OBSERVED, "executing", started_at=started)
        body = render_panel_body(snapshot)
        self.assertIn(
            "Новых наблюдаемых событий нет с 14:22. Это не означает остановку.",
            body,
        )
        self.assertNotIn("После запуска новых событий пока не было", body)

        just_below = replace(snapshot, observed_at=OBSERVED - timedelta(seconds=1))
        body = render_panel_body(just_below)
        self.assertNotIn("Новых наблюдаемых событий нет с", body)
        self.assertIn("После запуска новых событий пока не было", body)

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
        self.assertIn("Новых наблюдаемых событий нет с 14:22.", body)
        self.assertIn("Промежуточное сообщение доставлено · 14:22", body)
        recent = replace(snapshot, interim_delivered_at=OBSERVED - timedelta(minutes=9))
        self.assertNotIn("Новых наблюдаемых событий нет с", render_panel_body(recent))
        self.assertNotIn("После запуска новых событий пока не было", render_panel_body(recent))
        self.assertNotIn(
            "Промежуточное сообщение доставлено",
            render_panel_body(replace(snapshot, headline="needs_owner")),
        )

    def test_queue_with_and_without_held(self) -> None:
        snapshot = PanelSnapshot(OBSERVED, "queued", queued=3)
        self.assertIn("Очередь: 3", render_panel_body(snapshot).splitlines())
        self.assertIn(
            "Очередь: 3 · удержано: 2",
            render_panel_body(replace(snapshot, held=2)).splitlines(),
        )
        self.assertIn(
            "Очередь: 0 · удержано: 2",
            render_panel_body(replace(snapshot, queued=0, held=2)).splitlines(),
        )
        self.assertNotIn("Очередь:", render_panel_body(replace(snapshot, queued=0)))

    def test_idle_results(self) -> None:
        snapshot = PanelSnapshot(OBSERVED, "idle", last_result="delivered")
        self.assertIn("Последний ответ отправлен", render_panel_body(snapshot).splitlines())
        self.assertIn(
            "Последний ответ отправлен · 14:32",
            render_panel_body(replace(snapshot, last_result_at=OBSERVED)).splitlines(),
        )
        saved = replace(snapshot, last_result="saved", last_result_at=OBSERVED)
        self.assertIn(
            "Последний ответ сохранён, доставка не подтверждена",
            render_panel_body(saved).splitlines(),
        )
        self.assertNotIn("Последний ответ", render_panel_body(replace(saved, headline="queued")))

    def test_html_escaping(self) -> None:
        snapshot = PanelSnapshot(OBSERVED, "executing", agent='<b>&"', model='<b>&"')
        self.assertEqual(
            render_panel_body(snapshot),
            '🟢 Выполняется · &lt;b&gt;&amp;"\nМодель: &lt;b&gt;&amp;"',
        )

    def test_cross_day_and_explicit_timezone(self) -> None:
        snapshot = PanelSnapshot(
            OBSERVED,
            "idle",
            started_at=datetime(2026, 11, 19, 20, 59, tzinfo=UTC),
            last_result="delivered",
            last_result_at=datetime(2026, 11, 19, 21, 1, tzinfo=UTC),
        )
        body = render_panel_body(snapshot)
        self.assertIn("Запущено в 19.11 23:59", body)
        self.assertIn("Последний ответ отправлен · 00:01", body)
        utc_body = render_panel_body(snapshot, timezone=ZoneInfo("UTC"))
        self.assertIn("Запущено в 19.11 20:59", utc_body)
        self.assertIn("Последний ответ отправлен · 19.11 21:01", utc_body)

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

    def test_bounds_drop_whole_lines_and_preserve_headline(self) -> None:
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
        self.assertEqual(
            body.splitlines(),
            [
                "🔴 Нужно ваше решение",
                "Исход задачи неизвестен — решение в уведомлении выше",
                "Доставка ответа не подтверждена — тема ждёт решения",
                "Агент ждёт разрешения",
            ],
        )

        huge_agent = PanelSnapshot(OBSERVED, "executing", agent="<" * MAX_PANEL_CHARACTERS)
        self.assertEqual(render_panel_body(huge_agent), "🟢 Выполняется")

    def test_digest_stability_without_threshold_crossing(self) -> None:
        snapshot = PanelSnapshot(OBSERVED, "executing", started_at=OBSERVED - timedelta(minutes=2))
        body = render_panel_body(snapshot)
        self.assertEqual(panel_body_digest(body), hashlib.sha256(body.encode("utf-8")).hexdigest())
        self.assertEqual(panel_body_digest(body), panel_body_digest(render_panel_body(snapshot)))
        later = replace(snapshot, observed_at=OBSERVED + timedelta(minutes=1))
        self.assertEqual(body, render_panel_body(later))
        self.assertEqual(panel_body_digest(body), panel_body_digest(render_panel_body(later)))


if __name__ == "__main__":
    unittest.main()
