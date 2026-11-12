from __future__ import annotations

import json
import os
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

from hermes_codex_router.claude_stream import ClaudeStreamReader
from hermes_codex_router.claude_usage import (
    LARGE_CONTEXT_TOKENS,
    ClaudeUsageObserver,
    usage_footer_details,
    usage_from_metadata,
    usage_metadata_json,
)
from hermes_codex_router.claude_usage_state import ClaudeUsageStatus
from hermes_codex_router.codex_appserver import RateLimits
from hermes_codex_router.state import HubState
from hermes_codex_router.status_view import format_session_status

SESSION = "00000000-0000-4000-8000-000000000001"


def assistant(
    message_id: str, usage: Mapping[str, object] | None = None, *, parent: str | None = None
) -> dict[str, object]:
    return {
        "type": "assistant",
        "session_id": SESSION,
        "parent_tool_use_id": parent,
        "message": {"id": message_id, "content": [], "usage": dict(usage or {})},
    }


def request_usage(
    *, input: int = 5, read: int = 0, write: int = 0, w5m: int = 0, w1h: int = 0, out: int = 10
) -> dict[str, object]:
    return {
        "input_tokens": input,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write,
        "cache_creation": {"ephemeral_5m_input_tokens": w5m, "ephemeral_1h_input_tokens": w1h},
        "output_tokens": out,
    }


def summarize(*events: Mapping[str, object]) -> dict[str, object] | None:
    observer = ClaudeUsageObserver()
    for event in events:
        observer.observe(dict(event))
    return observer.summary()


class ClaudeUsageObserverTests(unittest.TestCase):
    def test_repeated_blocks_of_one_request_are_counted_once_with_latest_usage(self) -> None:
        summary = summarize(
            assistant("msg-1", request_usage(read=1000, write=200_000, w1h=200_000, out=1)),
            assistant("msg-1", request_usage(read=1000, write=200_000, w1h=200_000, out=80)),
            assistant("msg-2", request_usage(read=201_000, write=500, w1h=500, out=20)),
        )
        assert summary is not None
        self.assertEqual(summary["requests"], 2)
        self.assertEqual(summary["output"], 100)
        self.assertEqual(summary["cache_write"], 200_500)
        self.assertEqual(summary["cache_write_1h"], 200_500)
        self.assertEqual(summary["cache_write_5m"], 0)
        self.assertEqual(summary["cache_read"], 202_000)
        self.assertEqual(summary["last_context"], 5 + 201_000 + 500)
        self.assertEqual(summary["max_context"], 5 + 201_000 + 500)
        self.assertEqual(summary["large_cache_writes"], 1)

    def test_subagent_requests_do_not_define_main_context(self) -> None:
        summary = summarize(
            assistant("msg-1", request_usage(read=50_000)),
            assistant("sub-1", parent="toolu_1", usage=request_usage(read=900_000)),
        )
        assert summary is not None
        self.assertEqual((summary["requests"], summary["subagent_requests"]), (1, 1))
        self.assertEqual(summary["last_context"], 50_005)

    def test_compaction_after_last_request_makes_current_context_unknown(self) -> None:
        compacted = {"type": "system", "subtype": "compact_boundary", "session_id": SESSION}
        summary = summarize(assistant("msg-1", request_usage(read=300_000)), compacted)
        assert summary is not None
        self.assertIsNone(summary["last_context"])
        self.assertEqual(summary["max_context"], 300_005)
        summary = summarize(
            assistant("msg-1", request_usage(read=300_000)),
            compacted,
            assistant("msg-2", request_usage(write=30_000)),
        )
        assert summary is not None
        self.assertEqual(summary["last_context"], 30_005)

    def test_missing_fields_stay_unknown_not_zero(self) -> None:
        summary = summarize(
            assistant("msg-1", {"input_tokens": 5, "output_tokens": 3}),
            assistant("msg-2", request_usage(read=100)),
        )
        assert summary is not None
        self.assertIsNone(summary["cache_write"])
        self.assertIsNone(summary["cache_read"])
        self.assertEqual(summary["output"], 13)
        self.assertEqual(summary["last_context"], 105)
        self.assertIsNone(summarize({"type": "result", "session_id": SESSION}))
        boolean = summarize(assistant("msg-1", {**request_usage(), "input_tokens": True}))
        assert boolean is not None
        self.assertIsNone(boolean["last_context"])

    def test_a_later_request_without_usage_makes_context_unknown(self) -> None:
        summary = summarize(
            assistant("msg-1", request_usage(read=160_000)),
            {**assistant("msg-2"), "message": {"id": "msg-2", "content": []}},
        )
        assert summary is not None
        self.assertEqual(summary["requests"], 2)
        self.assertIsNone(summary["last_context"])
        self.assertIsNone(summary["cache_read"])

    def test_untracked_requests_beyond_the_bound_make_context_unknown(self) -> None:
        with patch("hermes_codex_router.claude_usage._MAX_TRACKED_REQUESTS", 1):
            summary = summarize(
                assistant("msg-1", request_usage(read=160_000)),
                assistant("msg-2", request_usage(read=10)),
            )
        assert summary is not None
        self.assertEqual(summary["requests"], 1)
        self.assertIsNone(summary["last_context"])

    def test_large_write_needs_at_least_fifty_thousand_written_tokens(self) -> None:
        # A fresh session's first write (~23k: system prompt and tools) is expected.
        small = summarize(assistant("msg-1", request_usage(input=15_000, write=23_000)))
        large = summarize(assistant("msg-1", request_usage(input=15_000, write=50_000)))
        assert small is not None and large is not None
        self.assertEqual((small["large_cache_writes"], large["large_cache_writes"]), (0, 1))

    def test_reader_publishes_summary_after_a_complete_drain_only(self) -> None:
        published: list[dict[str, object]] = []
        reader = ClaudeStreamReader(expected_session_id=SESSION, on_usage_summary=published.append)
        stream = "\n".join(
            json.dumps(event)
            for event in (
                assistant("msg-1", request_usage(write=40_000)),
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "session_id": SESSION,
                    "result": "done",
                },
            )
        )
        reader.feed(stream.encode())
        self.assertEqual(published, [])
        reader.finish()
        self.assertEqual(published[0]["last_context"], 40_005)

    def test_failing_callback_never_changes_the_turn(self) -> None:
        def broken(_: dict[str, object]) -> None:
            raise RuntimeError("fictional telemetry failure")

        reader = ClaudeStreamReader(expected_session_id=SESSION, on_usage_summary=broken)
        reader.feed((json.dumps(assistant("msg-1", request_usage())) + "\n").encode())
        reader.finish()


class ClaudeUsagePresentationTests(unittest.TestCase):
    def test_footer_reports_last_request_context_and_large_writes(self) -> None:
        details = usage_footer_details({"last_context": 238_400, "large_cache_writes": 2})
        self.assertEqual(details["Context"], "238k (last request)")
        self.assertEqual(details["Large cache writes"], "2")
        self.assertIn("/new", details["Notice"])
        small = usage_footer_details({"last_context": LARGE_CONTEXT_TOKENS - 1})
        self.assertNotIn("Notice", small)
        self.assertNotIn("Large cache writes", small)
        self.assertEqual(usage_footer_details(None), {"Context": "unavailable"})

    def test_metadata_round_trip_is_bounded_and_ignores_other_shapes(self) -> None:
        summary = summarize(*(assistant(f"msg-{i}", request_usage()) for i in range(3)))
        metadata = usage_metadata_json(summary)
        assert metadata is not None
        self.assertLessEqual(len(metadata), 4000)
        self.assertEqual(usage_from_metadata(metadata), summary)
        self.assertIsNone(usage_metadata_json(None))
        for other in (None, "", '{"hub_recovered":true}', "not json", "[]"):
            self.assertIsNone(usage_from_metadata(other))

    def test_status_shows_tokens_only_without_a_percentage(self) -> None:
        text = format_session_status(
            agent="Claude",
            model="claude-example",
            effort="high",
            writer="telegram",
            context_remaining=None,
            account_hint=None,
            limits=RateLimits(None, None),
            timezone_name="UTC",
            context_tokens=150_400,
        )
        self.assertIn("Context 150k (last request)", text)


class ClaudeUsageStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        base = Path(self.tempdir.name)
        root = base / "project"
        root.mkdir()
        self.state = HubState.open(base / "state.db", codex_permission_profile=None)
        self.topic = self.state.observe_topic(
            project_id="example-project",
            chat_id=-1001234567890,
            thread_id=7,
            title="Fictional topic",
            execution_root=root,
        )
        self.session = self.state.activate_agent(
            self.topic.topic_id, "claude", "claude-example", "high"
        )
        self.messages = 0

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def leased_job(self):
        self.messages += 1
        self.state.enqueue_provider_job(
            idempotency_key=f"usage:{self.messages}",
            chat_id=self.topic.chat_id,
            message_id=self.messages,
            topic_id=self.topic.topic_id,
            agent_id="claude",
            session_id=self.session.session_id,
            session_generation=self.session.generation,
            model=self.session.model,
            effort=self.session.effort,
            payload_text="Fictional request",
        )
        job = self.state.lease_provider_job("claude", "claude-worker")
        assert job is not None and job.lease_token is not None
        self.state.mark_provider_job_executing(job.job_id, job.lease_token)
        return job

    def complete(self, metadata: str | None) -> None:
        job = self.leased_job()
        assert job.lease_token is not None
        self.state.commit_provider_result(
            job.job_id,
            job.lease_token,
            visible_response="Fictional answer",
            sender_agent_id="claude",
            telegram_html="Fictional answer",
            provider_session_id=SESSION,
            safe_metadata_json=metadata,
        )
        # Simulate confirmed delivery so the topic FIFO can lease the next turn.
        with self.state._connection:
            self.state._connection.execute(
                "UPDATE provider_jobs SET status = 'completed' WHERE job_id = ?", (job.job_id,)
            )
            self.state._connection.execute(
                "UPDATE telegram_outbox SET status = 'delivered' WHERE job_id = ?", (job.job_id,)
            )

    def test_latest_finished_turn_decides_and_failures_make_it_unknown(self) -> None:
        status = ClaudeUsageStatus(self.state)
        self.assertIsNone(status.last_context(self.session.session_id))
        self.complete(usage_metadata_json({"last_context": 120_000}))
        self.assertEqual(status.last_context(self.session.session_id), 120_000)
        failed = self.leased_job()
        assert failed.lease_token is not None
        self.state.fail_provider_job(
            failed.job_id,
            failed.lease_token,
            error_class="quota",
            error_code="claude_rate_limited",
        )
        self.assertIsNone(status.last_context(self.session.session_id))
        self.complete(None)
        self.assertIsNone(status.last_context(self.session.session_id))


class ClaudeCacheTtlDefaultTests(unittest.TestCase):
    def test_turn_environment_defaults_to_one_hour_but_respects_the_service(self) -> None:
        from hermes_codex_router.external_runtime import (
            CLAUDE_PROMPT_CACHE_TTL,
            DEFAULT_CLAUDE_PROMPT_CACHE_TTL,
            ExternalCliAdapter,
        )

        runtime = ExternalCliAdapter("claude")
        saved = os.environ.pop(CLAUDE_PROMPT_CACHE_TTL, None)
        route = patch("hermes_codex_router.external_runtime._claude_route_environment")
        route.start()
        self.addCleanup(route.stop)
        try:
            environment = runtime._turn_environment(None, None)
            self.assertEqual(environment[CLAUDE_PROMPT_CACHE_TTL], DEFAULT_CLAUDE_PROMPT_CACHE_TTL)
            self.assertEqual(DEFAULT_CLAUDE_PROMPT_CACHE_TTL, "1h")
            os.environ[CLAUDE_PROMPT_CACHE_TTL] = "5m"
            self.assertEqual(runtime._turn_environment(None, None)[CLAUDE_PROMPT_CACHE_TTL], "5m")
            other = ExternalCliAdapter("opencode")
            os.environ.pop(CLAUDE_PROMPT_CACHE_TTL)
            self.assertNotIn(CLAUDE_PROMPT_CACHE_TTL, other._turn_environment(None, None))
        finally:
            os.environ.pop(CLAUDE_PROMPT_CACHE_TTL, None)
            if saved is not None:
                os.environ[CLAUDE_PROMPT_CACHE_TTL] = saved


if __name__ == "__main__":
    unittest.main()
