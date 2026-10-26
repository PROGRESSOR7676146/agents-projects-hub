from __future__ import annotations

import unittest

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
