from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hermes_codex_router.worker_process_lock import (
    WorkerProcessLockError,
    worker_process_lock,
)


class WorkerProcessLockTests(unittest.TestCase):
    def test_duplicate_slot_is_rejected_and_released_slot_can_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "example-state.db"
            with worker_process_lock(state_path, "codex-worker-2"):
                with self.assertRaisesRegex(WorkerProcessLockError, "already running"):
                    with worker_process_lock(state_path, "codex-worker-2"):
                        self.fail("duplicate slot was admitted")
                with worker_process_lock(state_path, "codex-worker-3"):
                    pass
            with worker_process_lock(state_path, "codex-worker-2"):
                pass

    def test_worker_identity_cannot_escape_lock_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(WorkerProcessLockError, "invalid worker identity"):
                with worker_process_lock(Path(directory) / "example-state.db", "../other"):
                    pass
