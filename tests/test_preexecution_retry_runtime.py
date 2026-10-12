from __future__ import annotations

import unittest
from dataclasses import replace
from typing import Any, cast
from unittest.mock import Mock, patch

from hermes_codex_router.execution_journal import ExecutionJournal
from hermes_codex_router.external_worker import ExternalQueueWorker
from tests import test_preexecution_retry as retry_fixtures
from tests.hub_service_harness import CODEX


class PreparationRetryRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = retry_fixtures.PreexecutionRetryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.child = self.fixture.alias_retry_child()
        self.harness = self.fixture.harness

    def assert_refused_before_preparation(self, *, runtime: str, external: bool) -> None:
        alias = replace(
            self.harness.service.config.require_agent(self.child.agent_id), runtime=runtime
        )
        self.harness.with_config(
            agents=(CODEX, alias),
            queue_runtime="external" if external else "embedded",
            outbox_runtime="external",
            external_worker_agent_ids=("codex", alias.agent_id) if external else (),
        )
        config = self.harness.service.config
        if external:
            worker = ExternalQueueWorker(
                config,
                agent_id=alias.agent_id,
                registry=self.harness.registry,
                adapter=cast(Any, Mock()),
            )
            self.addCleanup(worker.close)
            with patch.object(
                worker, "_execute_external", side_effect=AssertionError("unexpected dispatch")
            ) as preparation:
                self.assertTrue(worker.run_cycle())
        else:
            with patch(
                "hermes_codex_router.service.prepare_worker_materials",
                side_effect=AssertionError("unexpected preparation"),
            ) as preparation:
                self.assertTrue(self.harness.service.run_embedded_queue_cycle())
        preparation.assert_not_called()
        state = self.fixture.state
        failed = state.get_provider_job(self.child.job_id)
        self.assertEqual((failed.status, failed.error_class), ("failed", "pre_execution"))
        self.assertEqual(failed.error_code, "CodexRetryBindingError")
        self.assertEqual(failed.payload_text, self.fixture.payload)
        self.assertIsNone(ExecutionJournal(state).read(self.child.job_id))
        notice = state.get_telegram_outbox_for_job(self.child.job_id).telegram_html
        self.assertIn("The provider was not started", notice)
        self.assertNotIn("may already have", notice)
        self.assertNotIn("Reply exactly retry", notice)
        self.assertIsNone(
            self.fixture.sql(
                "SELECT 1 FROM provider_preexecution_retry_tickets WHERE source_job_id=?",
                (self.child.job_id,),
            ).fetchone()
        )

    def test_external_opencode_reconfiguration_refuses_saved_codex_retry(self) -> None:
        self.assert_refused_before_preparation(runtime="opencode", external=True)

    def test_external_antigravity_reconfiguration_refuses_saved_codex_retry(self) -> None:
        self.assert_refused_before_preparation(runtime="antigravity", external=True)

    def test_embedded_opencode_reconfiguration_refuses_saved_codex_retry(self) -> None:
        self.assert_refused_before_preparation(runtime="opencode", external=False)

    def test_embedded_antigravity_reconfiguration_refuses_saved_codex_retry(self) -> None:
        self.assert_refused_before_preparation(runtime="antigravity", external=False)


if __name__ == "__main__":
    unittest.main()
