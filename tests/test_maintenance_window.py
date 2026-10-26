from __future__ import annotations

import os
import socket
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from hermes_codex_router import unix_listener_custody as custody
from hermes_codex_router.maintenance_window import (
    MaintenanceWindow,
    Observation,
    RestorationError,
    Unit,
)


class MaintenanceWindowTests(unittest.TestCase):
    def setUp(self):
        self.states: dict[str, Observation] = {
            "worker": "active",
            "sender": "active",
            "admission": "active",
            "unused": "inactive",
        }
        self.events = []
        self.fail_start = set()
        self.unsafe = set()
        self.units = tuple(self.unit(name) for name in self.states)

    def unit(self, name):
        def stop():
            self.events.append(("stop", name))
            self.states[name] = "inactive"

        def start():
            self.events.append(("start", name))
            if name in self.fail_start:
                raise TimeoutError("unknown start")
            self.states[name] = "active"

        def prerequisite():
            if name in self.unsafe:
                raise ValueError("native custody unknown")

        def ready():
            self.events.append(("ready", name))
            if self.states[name] != "active":
                raise ValueError("not ready")

        return Unit(
            name, name == "admission", lambda: self.states[name], stop, start, prerequisite, ready
        )

    def test_diagnostic_failure_restores_consumers_before_admission_and_preserves_error(self):
        window = MaintenanceWindow(self.units)
        failure = ValueError("post-attach diagnostic failed")
        with self.assertRaises(ValueError) as caught, window:
            for name in ("admission", "worker", "sender", "unused"):
                window.stop(name)
            raise failure
        self.assertIs(caught.exception, failure)
        self.assertTrue(window.report.complete)
        self.assertEqual(
            [name for action, name in self.events if action == "start"],
            ["worker", "sender", "admission"],
        )
        self.assertEqual(self.states["unused"], "inactive")

    def test_real_post_attach_sequence_failure_returns_consumers_and_input(self):
        # Disposable sockets and fictional units exercise the combined sequence,
        # including the connection state that the former pathname counter rejected.
        for lose_listener in (False, True):
            with self.subTest(lose_listener=lose_listener):
                self.setUp()
                with (
                    tempfile.TemporaryDirectory() as temp,
                    socket.socket(socket.AF_UNIX) as listener,
                    socket.socket(socket.AF_UNIX) as companion,
                ):
                    path = Path(temp) / "native.sock"
                    listener.bind(str(path))
                    listener.listen(1)
                    listener.settimeout(2)
                    companion.settimeout(2)
                    owner = custody.process_identity(os.getpid())
                    inode = custody.verify_listener(path, owner)

                    def require_native_custody():
                        if custody.verify_listener(path, owner) != inode:
                            raise ValueError("listener changed")

                    window = MaintenanceWindow(
                        (
                            replace(self.units[0], prerequisite=require_native_custody),
                            *self.units[1:],
                        )
                    )
                    with (
                        self.assertRaisesRegex(
                            ValueError if lose_listener else RuntimeError,
                            "missing" if lose_listener else "fictional post-attach check failed",
                        ) as caught,
                        window,
                    ):
                        for name in ("admission", "worker", "sender"):
                            window.stop(name)
                        companion.connect(str(path))
                        with listener.accept()[0]:
                            self.assertEqual(custody.verify_listener(path, owner), inode)
                            if lose_listener:
                                listener.close()
                                custody.verify_listener(path, owner)
                                self.fail("closed listener must refuse")
                            raise RuntimeError("fictional post-attach check failed")
                    if lose_listener:
                        self.assertFalse(window.report.complete)
                        self.assertNotIn(("start", "worker"), self.events)
                        self.assertEqual(self.states["sender"], "active")
                        self.assertEqual(self.states["admission"], "inactive")
                        self.assertIn("restoration incomplete", caught.exception.__notes__[0])
                    else:
                        self.assertTrue(window.report.complete)
                        self.assertEqual(
                            [name for action, name in self.events if action == "start"],
                            ["worker", "sender", "admission"],
                        )
                    self.assertEqual(self.states["unused"], "inactive")

    def test_one_unknown_start_does_not_skip_sender_but_blocks_admission_without_retry(self):
        window = MaintenanceWindow(self.units)
        self.fail_start.add("worker")
        with self.assertRaises(RestorationError), window:
            for name in ("admission", "worker", "sender"):
                window.stop(name)
        self.assertFalse(window.report.complete)
        self.assertEqual(self.states["sender"], "active")
        self.assertEqual(self.states["admission"], "inactive")
        self.assertEqual(self.events.count(("start", "worker")), 1)
        before = list(self.events)
        window.restore()
        self.assertEqual(self.events, before)

    def test_changed_native_custody_blocks_effects_but_independent_sender_recovers(self):
        window = MaintenanceWindow(self.units)
        with self.assertRaises(RestorationError), window:
            for name in ("admission", "worker", "sender"):
                window.stop(name)
            self.unsafe.add("worker")
        self.assertNotIn(("start", "worker"), self.events)
        self.assertEqual(self.states["sender"], "active")
        self.assertEqual(self.states["admission"], "inactive")

    def test_already_running_consumer_is_verified_without_duplicate_start(self):
        with MaintenanceWindow(self.units) as window:
            window.stop("admission")
            window.stop("worker")
            self.states["worker"] = "active"
        self.assertTrue(window.report.complete)
        self.assertNotIn(("start", "worker"), self.events)
        self.assertIn(("ready", "worker"), self.events)

    def test_unstopped_consumer_failure_blocks_admission_without_starting_it(self):
        window = MaintenanceWindow(self.units)
        with self.assertRaises(RestorationError), window:
            window.stop("admission")
            self.states["worker"] = "inactive"
        self.assertNotIn(("start", "worker"), self.events)
        self.assertEqual(self.states["admission"], "inactive")

    def test_failed_readiness_and_primary_failure_are_both_retained(self):
        from dataclasses import replace

        def not_ready():
            raise ValueError("wrong effective config")

        window = MaintenanceWindow((replace(self.units[0], ready=not_ready), *self.units[1:]))
        primary = RuntimeError("diagnostic failure")
        with self.assertRaises(RuntimeError) as caught, window:
            for name in ("admission", "worker", "sender"):
                window.stop(name)
            raise primary
        self.assertIs(caught.exception, primary)
        self.assertIn("restoration incomplete", primary.__notes__[0])
        self.assertFalse(window.report.complete)
        self.assertEqual(self.states["sender"], "active")
        self.assertEqual(self.states["admission"], "inactive")

    def test_timed_out_start_with_positive_readiness_is_observed_without_retry(self):
        from dataclasses import replace

        def uncertain_start():
            self.events.append(("start", "worker"))
            self.states["worker"] = "active"
            raise TimeoutError()

        with MaintenanceWindow(
            (replace(self.units[0], start=uncertain_start), *self.units[1:])
        ) as w:
            w.stop("admission")
            w.stop("worker")
        self.assertTrue(w.report.complete)
        self.assertEqual(self.events.count(("start", "worker")), 1)

    def test_duplicate_stop_and_window_reuse_refused_without_effect(self):
        window = MaintenanceWindow(self.units)
        with window:
            window.stop("admission")
            with self.assertRaises(ValueError):
                window.stop("admission")
        with self.assertRaises(ValueError):
            window.__enter__()
        self.assertEqual(self.events.count(("stop", "admission")), 1)

    def test_unknown_observation_never_starts_unit(self):
        window = MaintenanceWindow(self.units)
        with self.assertRaises(RestorationError), window:
            window.stop("admission")
            window.stop("worker")
            self.states["worker"] = "unknown"
        self.assertNotIn(("start", "worker"), self.events)
        self.assertEqual(self.states["admission"], "inactive")

    def test_partially_failed_stop_is_finalized(self):
        def partial_stop():
            self.states["worker"] = "inactive"
            raise TimeoutError("stop outcome requires observation")

        worker = self.units[0]
        from dataclasses import replace

        window = MaintenanceWindow((replace(worker, stop=partial_stop), *self.units[1:]))
        with self.assertRaises(TimeoutError), window:
            window.stop("admission")
            window.stop("worker")
        self.assertTrue(window.report.complete)
        self.assertEqual(self.states["worker"], "active")

    def test_uncaught_base_exception_still_finalizes(self):
        with self.assertRaises(KeyboardInterrupt), MaintenanceWindow(self.units) as window:
            window.stop("admission")
            window.stop("worker")
            raise KeyboardInterrupt()
        self.assertTrue(window.report.complete)

    def test_interrupt_inside_finalization_leaves_incomplete_report_without_retry(self):
        from dataclasses import replace

        def interrupted_start():
            self.events.append(("start", "worker"))
            raise KeyboardInterrupt()

        window = MaintenanceWindow(
            (replace(self.units[0], start=interrupted_start), *self.units[1:])
        )
        with self.assertRaises(KeyboardInterrupt), window:
            window.stop("admission")
            window.stop("worker")
        self.assertFalse(window.report.complete)
        self.assertEqual(window.report.phase, "interrupted")
        self.assertIn("worker", window.report.stop_attempts)
        self.assertEqual(dict(window.report.initial)["worker"], "active")
        before = list(self.events)
        self.assertFalse(window.restore().complete)
        self.assertEqual(self.events, before)

    def test_interrupt_during_restore_does_not_replace_primary_error(self):
        from dataclasses import replace

        def interrupted_ready():
            raise SystemExit(1)

        window = MaintenanceWindow(
            (replace(self.units[0], ready=interrupted_ready), *self.units[1:])
        )
        primary = ValueError("original diagnostic failure")
        with self.assertRaises(ValueError) as caught, window:
            window.stop("admission")
            window.stop("worker")
            raise primary
        self.assertIs(caught.exception, primary)
        self.assertFalse(window.report.complete)
        self.assertEqual(window.report.interruption, "SystemExit")
        self.assertIn("interrupted", primary.__notes__[0])

    def test_unknown_unit_is_explicit_value_error_without_effect(self):
        with MaintenanceWindow(self.units) as window, self.assertRaisesRegex(ValueError, "unknown"):
            window.stop("not-configured")
        self.assertEqual(self.events, [])

    def test_unknown_initial_snapshot_and_consumer_stop_before_fence_are_refused(self):
        self.states["worker"] = "unknown"
        with self.assertRaises(ValueError), MaintenanceWindow(self.units):
            self.fail("body must not run")
        self.assertEqual(self.events, [])
        self.states["worker"] = "active"
        with MaintenanceWindow(self.units) as window, self.assertRaises(ValueError):
            window.stop("worker")
        self.assertEqual(self.events, [])

    def test_initially_inactive_admission_becomes_active_or_unknown_blocks_consumer_stop(self):
        transitions: tuple[Observation, ...] = ("active", "unknown")
        for state in transitions:
            with self.subTest(state=state):
                self.setUp()
                self.states["admission"] = "inactive"
                window = MaintenanceWindow(self.units)
                with window, self.assertRaisesRegex(ValueError, "admission must be closed"):
                    self.states["admission"] = state
                    window.stop("worker")
                self.assertEqual(self.events, [])
