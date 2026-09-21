from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from hermes_codex_router.state import HubState, RuntimeHealthRecord, RuntimeHealthStatus
from hermes_codex_router.state_runtime_health import (
    RuntimeHealthRecord as FacadeRuntimeHealthRecord,
)
from hermes_codex_router.state_runtime_health import (
    RuntimeHealthStatus as FacadeRuntimeHealthStatus,
)


class RuntimeHealthStateFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base = Path(self.tempdir.name)
        self.state = HubState.open(self.base / "private" / "hub.db")
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.state.close()
        self.tempdir.cleanup()

    def test_facade_uses_hub_connection_and_preserves_public_record_types(self) -> None:
        self.assertIs(self.state._runtime_health_state._connection, self.state._connection)
        self.assertEqual(
            self.state._runtime_health_state._write_transaction,
            self.state._connection_transaction,
        )
        self.assertIs(RuntimeHealthRecord, FacadeRuntimeHealthRecord)
        self.assertIs(RuntimeHealthStatus, FacadeRuntimeHealthStatus)

    def test_health_write_fault_rolls_back_through_hub_transaction_owner(self) -> None:
        transaction = self.state._runtime_health_state._write_transaction

        @contextmanager
        def fail_after_write() -> Iterator[None]:
            with transaction():
                yield
                raise RuntimeError("fictional post-health-write fault")

        with patch.object(
            self.state._runtime_health_state,
            "_write_transaction",
            fail_after_write,
        ):
            with self.assertRaisesRegex(RuntimeError, "post-health-write fault"):
                self.state.upsert_runtime_health(
                    component="controller",
                    instance_id="fictional-controller",
                    pid=1234,
                    process_start_marker="fictional-start",
                    started_at=self.now,
                    heartbeat_at=self.now,
                )

        self.assertIsNone(self.state.get_runtime_health("controller", "fictional-controller"))
        self.assertFalse(self.state._connection.in_transaction)


if __name__ == "__main__":
    unittest.main()
