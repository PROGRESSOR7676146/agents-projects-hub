from __future__ import annotations

import threading
import unittest

from hermes_codex_router.worker_activity_barrier import WorkerActivityBarrier


class WorkerActivityBarrierTests(unittest.TestCase):
    def test_drain_closes_admission_without_cancelling_existing_work(self) -> None:
        activity = WorkerActivityBarrier()
        entered = threading.Event()
        finish = threading.Event()

        def work() -> None:
            with activity.operation() as admitted:
                self.assertTrue(admitted)
                entered.set()
                self.assertTrue(finish.wait(2))
                # Work admitted before drain can finish its nested cleanup.
                with activity.operation() as nested:
                    self.assertTrue(nested)

        thread = threading.Thread(target=work)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            activity.request_drain()
            self.assertFalse(activity.drained)
            with activity.operation() as admitted:
                self.assertFalse(admitted)
        finally:
            finish.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(activity.drained)

    def test_idle_is_not_a_drain_receipt(self) -> None:
        activity = WorkerActivityBarrier()
        self.assertFalse(activity.drained)
        activity.request_drain()
        self.assertTrue(activity.drained)
        activity.request_drain()
        self.assertTrue(activity.drained)

    def test_child_outlives_parent_scope_and_join_timeout(self) -> None:
        activity = WorkerActivityBarrier()
        finish = threading.Event()
        with activity.operation():
            activity.request_drain()
            child = activity.thread(target=lambda: finish.wait(2), daemon=True)
            child.start()
        try:
            child.join(0.001)
            self.assertTrue(child.is_alive())
            self.assertFalse(activity.drained)
        finally:
            finish.set()
            child.join(2)
        self.assertTrue(activity.drained)

    def test_unstarted_child_is_denied_after_drain(self) -> None:
        activity = WorkerActivityBarrier()
        called = threading.Event()
        child = activity.thread(target=called.set)
        activity.request_drain()
        with self.assertRaisesRegex(RuntimeError, "draining"):
            child.start()
        self.assertFalse(called.is_set())
        self.assertTrue(activity.drained)

    def test_background_thread_cannot_inherit_admission_for_new_work(self) -> None:
        activity = WorkerActivityBarrier()
        ready = threading.Event()
        proceed = threading.Event()
        admitted: list[bool] = []

        def background() -> None:
            ready.set()
            self.assertTrue(proceed.wait(2))
            with activity.operation() as allowed:
                admitted.append(allowed)

        child = activity.thread(target=background)
        child.start()
        try:
            self.assertTrue(ready.wait(2))
            activity.request_drain()
            self.assertFalse(activity.drained)
        finally:
            proceed.set()
            child.join(2)
        self.assertEqual(admitted, [False])
        self.assertTrue(activity.drained)

    def test_unconfirmed_thread_start_retains_activity(self) -> None:
        from unittest.mock import patch

        activity = WorkerActivityBarrier()
        child = activity.thread(target=lambda: None)
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("start failed")):
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                child.start()
        activity.request_drain()
        self.assertFalse(activity.drained)

    def test_error_after_actual_start_cannot_release_the_parent_operation(self) -> None:
        from unittest.mock import patch

        activity = WorkerActivityBarrier()
        original_start = threading.Thread.start

        def start_then_raise(thread: threading.Thread) -> None:
            original_start(thread)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            raise RuntimeError("fictional failure after actual thread start")

        with activity.operation():
            child = activity.thread(target=lambda: None)
            with patch.object(threading.Thread, "start", start_then_raise):
                with self.assertRaisesRegex(RuntimeError, "after actual"):
                    child.start()
            activity.request_drain()
            self.assertFalse(activity.drained)
        self.assertTrue(activity.drained)

    def test_exception_releases_scope_without_reopening_admission(self) -> None:
        activity = WorkerActivityBarrier()
        with self.assertRaises(ValueError), activity.operation():
            activity.request_drain()
            raise ValueError("fictional operation failed")
        self.assertTrue(activity.drained)
        with activity.operation() as admitted:
            self.assertFalse(admitted)

    def test_start_window_pruning_retains_thread_until_physical_exit(self) -> None:
        from unittest.mock import patch

        original_start = threading.Thread.start
        for pruning in ("observer", "another_start"):
            with self.subTest(pruning=pruning):
                activity = WorkerActivityBarrier()
                first = activity.thread(target=lambda: None)
                second = activity.thread(target=lambda: None)

                def start(thread):
                    if thread is first:
                        activity.request_drain()
                        if pruning == "observer":
                            self.assertFalse(activity.drained)
                        else:
                            second.start()
                            second.join(2)
                            self.assertFalse(second.is_alive())
                    original_start(thread)

                with activity.operation(), patch.object(threading.Thread, "start", start):
                    first.start()
                    first.join(2)
                    self.assertFalse(first.is_alive())
                with patch.object(first, "is_alive", return_value=True):
                    self.assertFalse(activity.drained)
                self.assertTrue(activity.drained)


if __name__ == "__main__":
    unittest.main()
