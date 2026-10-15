from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from hermes_codex_router.acceptance_runtime import (
    AcceptanceRuntimeError,
    FixedServiceSupervisor,
    ReadOnlyAcceptanceState,
)
from hermes_codex_router.schema_compatibility import TARGET_SCHEMA_VERSION


class StatefulSystemctl:
    def __init__(
        self,
        *,
        controller_active: bool = True,
        worker_active: bool = True,
        fail_action: str | None = None,
        extra_worker_state: str | None = None,
        unload_stopped_worker: bool = False,
    ) -> None:
        self.active = {
            "agents-projects-hub.service": controller_active,
            "agents-projects-hub-worker@codex.service": worker_active,
        }
        self.fail_action = fail_action
        self.actions: list[str] = []
        self.probes: list[str] = []
        self.extra_worker_state = extra_worker_state
        self.unload_stopped_worker = unload_stopped_worker
        self.topology_probes: list[tuple[str, ...]] = []
        self.topology_sizes: list[int] = []

    def __call__(
        self, argv: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        action = argv[2]
        unit = argv[-1]
        if action == "list-units":
            self.topology_probes.append(argv)
            active = self.active["agents-projects-hub-worker@codex.service"]
            rows = [
                {
                    "unit": "agents-projects-hub-worker@codex.service",
                    "active": "active" if active else "inactive",
                }
            ]
            if self.unload_stopped_worker and not active:
                rows = []
            if self.extra_worker_state is not None:
                rows.append(
                    {
                        "unit": "agents-projects-hub-codex-worker@2.service",
                        "active": self.extra_worker_state,
                    }
                )
            self.topology_sizes.append(len(rows))
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(rows))
        if action == "is-active":
            self.probes.append(unit)
            return subprocess.CompletedProcess(argv, 0 if self.active[unit] else 3)
        name = f"{action}:{unit}"
        self.actions.append(name)
        if name == self.fail_action:
            raise subprocess.CalledProcessError(1, argv)
        if action == "stop":
            self.active[unit] = False
        elif action in {"start", "restart"}:
            self.active[unit] = True
        return subprocess.CompletedProcess(argv, 0)


class AcceptanceStateProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tempdir.name) / "state.db"
        connection = sqlite3.connect(self.state_path)
        connection.executescript(
            f"""
            PRAGMA user_version={TARGET_SCHEMA_VERSION};
            CREATE TABLE provider_jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE provider_job_inputs (
                job_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL
            );
            CREATE TABLE incoming_materials (
                job_id TEXT NOT NULL
            );
            INSERT INTO provider_jobs VALUES ('job-1', 'completed', '2026-01-01');
            INSERT INTO provider_job_inputs VALUES ('job-1', -1000000000001, 41);
            INSERT INTO incoming_materials VALUES ('job-1');
            INSERT INTO incoming_materials VALUES ('job-1');
            """
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_reads_bounded_job_membership_and_material_cardinality(self) -> None:
        probe = ReadOnlyAcceptanceState(self.state_path)

        self.assertEqual(probe.jobs_for_input(-1000000000001, 41), [("job-1", "completed")])
        self.assertEqual(probe.material_count("job-1"), 2)

        connection = sqlite3.connect(self.state_path)
        try:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM provider_jobs").fetchone(), (1,)
            )
        finally:
            connection.close()

    def test_rejects_state_before_incoming_material_schema_without_exposing_path(self) -> None:
        connection = sqlite3.connect(self.state_path)
        connection.execute("PRAGMA user_version=32")
        connection.commit()
        connection.close()

        with self.assertRaises(AcceptanceRuntimeError) as raised:
            ReadOnlyAcceptanceState(self.state_path).jobs_for_input(-1000000000001, 41)

        self.assertNotIn(str(self.state_path), str(raised.exception))
        self.assertIn("schema", str(raised.exception))

    def test_rejects_future_state_schema_without_exposing_path(self) -> None:
        connection = sqlite3.connect(self.state_path)
        connection.execute(f"PRAGMA user_version={TARGET_SCHEMA_VERSION + 1}")
        connection.commit()
        connection.close()

        with self.assertRaises(AcceptanceRuntimeError) as raised:
            ReadOnlyAcceptanceState(self.state_path).jobs_for_input(-1000000000001, 41)

        self.assertNotIn(str(self.state_path), str(raised.exception))
        self.assertIn("schema", str(raised.exception))

    def test_limits_job_membership_to_two_rows(self) -> None:
        connection = sqlite3.connect(self.state_path)
        connection.executescript(
            """
            INSERT INTO provider_jobs VALUES ('job-2', 'queued', '2026-01-02');
            INSERT INTO provider_jobs VALUES ('job-3', 'queued', '2026-01-03');
            INSERT INTO provider_job_inputs VALUES ('job-2', -1000000000001, 41);
            INSERT INTO provider_job_inputs VALUES ('job-3', -1000000000001, 41);
            """
        )
        connection.commit()
        connection.close()

        rows = ReadOnlyAcceptanceState(self.state_path).jobs_for_input(-1000000000001, 41)

        self.assertEqual(rows, [("job-1", "completed"), ("job-2", "queued")])

    def test_limits_material_cardinality_to_three_rows(self) -> None:
        connection = sqlite3.connect(self.state_path)
        connection.executemany(
            "INSERT INTO incoming_materials VALUES (?)",
            [("job-1",), ("job-1",)],
        )
        connection.commit()
        connection.close()

        self.assertEqual(ReadOnlyAcceptanceState(self.state_path).material_count("job-1"), 3)


class FixedServiceSupervisorTests(unittest.TestCase):
    def test_stopped_boundary_accepts_unloaded_fixed_unit_but_still_vetoes_consumers(self) -> None:
        for payload in (
            [],
            [{"unit": "agents-projects-hub-codex-worker@2.service", "active": "inactive"}],
        ):
            run = Mock(return_value=subprocess.CompletedProcess((), 0, stdout=json.dumps(payload)))
            supervisor = FixedServiceSupervisor(run=run)
            supervisor.require_single_codex_worker(worker_stopped=True)
            with self.assertRaisesRegex(AcceptanceRuntimeError, "topology"):
                supervisor.require_single_codex_worker()
        for unit, detail in (
            ("agents-projects-hub-codex-worker@2.service", "single Codex worker"),
            ("agents-projects-hub-worker@codex.service", "did not stay stopped"),
        ):
            for state in ("active", "activating", "reloading", "deactivating", "unknown"):
                with self.subTest(unit=unit, state=state):
                    run = Mock(
                        return_value=subprocess.CompletedProcess(
                            (), 0, stdout=json.dumps([{"unit": unit, "active": state}])
                        )
                    )
                    with self.assertRaisesRegex(AcceptanceRuntimeError, detail):
                        FixedServiceSupervisor(run=run).require_single_codex_worker(
                            worker_stopped=True
                        )

    def test_accepts_only_quiescent_numbered_workers_without_service_actions(self) -> None:
        for state in (None, "inactive", "failed"):
            with self.subTest(state=state):
                systemctl = StatefulSystemctl(extra_worker_state=state)
                supervisor = FixedServiceSupervisor(run=systemctl)
                supervisor.require_single_codex_worker()
                self.assertEqual(systemctl.actions, [])
                self.assertEqual(len(systemctl.topology_probes), 1)
                self.assertEqual(
                    systemctl.topology_probes[0][-2:],
                    (
                        "agents-projects-hub-worker@codex.service",
                        "agents-projects-hub-codex-worker@*.service",
                    ),
                )

    def test_rejects_additional_consumers_and_transitional_states_without_stopping_them(
        self,
    ) -> None:
        for state in ("active", "activating", "reloading", "deactivating", "unknown"):
            with self.subTest(state=state):
                systemctl = StatefulSystemctl(extra_worker_state=state)
                with self.assertRaisesRegex(AcceptanceRuntimeError, "single Codex worker"):
                    FixedServiceSupervisor(run=systemctl).require_single_codex_worker()
                self.assertEqual(systemctl.actions, [])

    def test_topology_errors_fail_closed_without_exposing_service_output(self) -> None:
        primary = {"unit": "agents-projects-hub-worker@codex.service", "active": "active"}
        invalid = (
            "private service output",
            "{}",
            "[]",
            "[null]",
            "[1]",
            "[{}]",
            json.dumps([primary, primary]),
            json.dumps([primary, {"unit": "unrelated-private.service", "active": "active"}]),
            json.dumps([{**primary, "active": None}]),
            " " * 65537,
        )
        for payload in invalid:
            with self.subTest(payload=payload[:80]):
                run = Mock(return_value=subprocess.CompletedProcess((), 0, stdout=payload))
                with self.assertRaisesRegex(AcceptanceRuntimeError, "topology") as raised:
                    FixedServiceSupervisor(run=run).require_single_codex_worker()
                self.assertNotIn("private", str(raised.exception))
                self.assertEqual(run.call_count, 1)

    def test_unavailable_topology_probe_fails_closed(self) -> None:
        for error in (
            OSError("private"),
            subprocess.TimeoutExpired("private", 10),
            subprocess.CalledProcessError(1, "private"),
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "private"),
        ):
            with self.subTest(error=type(error).__name__):
                run = Mock(side_effect=error)
                with self.assertRaisesRegex(AcceptanceRuntimeError, "topology") as raised:
                    FixedServiceSupervisor(run=run).require_single_codex_worker()
                self.assertNotIn("private", str(raised.exception))

    def test_captures_and_restores_only_the_fixed_initially_active_units(self) -> None:
        systemctl = StatefulSystemctl(controller_active=True, worker_active=False)
        supervisor = FixedServiceSupervisor(run=systemctl)
        initial = supervisor.capture_active_state()

        supervisor.restart_controller()
        supervisor.restore(initial)

        self.assertEqual(
            systemctl.actions,
            ["restart:agents-projects-hub.service"],
        )
        self.assertEqual(
            systemctl.probes,
            [
                "agents-projects-hub.service",
                "agents-projects-hub-worker@codex.service",
                "agents-projects-hub.service",
            ],
        )
        self.assertFalse(systemctl.active["agents-projects-hub-worker@codex.service"])

    def test_restoration_attempts_every_initially_active_unit_and_reports_failure(self) -> None:
        systemctl = StatefulSystemctl(fail_action="start:agents-projects-hub-worker@codex.service")
        supervisor = FixedServiceSupervisor(run=systemctl)
        initial = supervisor.capture_active_state()
        supervisor.stop_codex_worker()

        with self.assertRaisesRegex(AcceptanceRuntimeError, "restoration"):
            supervisor.restore(initial)

        self.assertEqual(
            systemctl.actions,
            [
                "stop:agents-projects-hub-worker@codex.service",
                "start:agents-projects-hub-worker@codex.service",
            ],
        )


if __name__ == "__main__":
    unittest.main()
