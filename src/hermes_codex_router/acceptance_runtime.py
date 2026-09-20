from __future__ import annotations

import sqlite3
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CONTROLLER_UNIT = "agents-projects-hub.service"
_CODEX_WORKER_UNIT = "agents-projects-hub-worker@codex.service"
_INCOMING_MATERIAL_SCHEMA = 33


class AcceptanceRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ServiceSnapshot:
    controller_active: bool
    codex_worker_active: bool

    @property
    def all_active(self) -> bool:
        return self.controller_active and self.codex_worker_active


class ReadOnlyAcceptanceState:
    """Bounded live-acceptance reads against the schema-33 state database."""

    def __init__(self, path: Path) -> None:
        self._path = path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(self._path.resolve().as_uri() + "?mode=ro", uri=True)
            connection.execute("PRAGMA query_only=ON")
            schema_row = connection.execute("PRAGMA user_version").fetchone()
            schema = int(schema_row[0]) if schema_row is not None else 0
            if schema < _INCOMING_MATERIAL_SCHEMA:
                raise AcceptanceRuntimeError(
                    "acceptance state schema does not support incoming materials"
                )
            yield connection
        except AcceptanceRuntimeError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise AcceptanceRuntimeError(
                f"acceptance state probe failed: {type(exc).__name__}"
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    def jobs_for_input(self, chat_id: int, message_id: int) -> list[tuple[str, str]]:
        with self._connection() as connection:
            return [
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT jobs.job_id,jobs.status FROM provider_job_inputs inputs "
                    "JOIN provider_jobs jobs ON jobs.job_id=inputs.job_id "
                    "WHERE inputs.chat_id=? AND inputs.message_id=? ORDER BY jobs.created_at "
                    "LIMIT 2",
                    (chat_id, message_id),
                )
            ]

    def material_count(self, job_id: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM incoming_materials WHERE job_id=?", (job_id,)
            ).fetchone()
            return int(row[0]) if row is not None else 0


class FixedServiceSupervisor:
    """Controls only the two fixed units used by the P0/P1 acceptance scenario."""

    def __init__(
        self,
        *,
        run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
    ) -> None:
        self._run = run

    def _active(self, unit: str) -> bool:
        try:
            result = self._run(
                ("systemctl", "--user", "is-active", "--quiet", unit),
                check=False,
                timeout=10,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AcceptanceRuntimeError(
                "service state check failed for the fixed acceptance unit"
            ) from exc
        return result.returncode == 0

    def _action(self, action: str, unit: str) -> None:
        try:
            self._run(
                ("systemctl", "--user", action, unit),
                check=True,
                timeout=40,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise AcceptanceRuntimeError(
                f"service {action} failed for the fixed acceptance unit"
            ) from exc

    def capture_active_state(self) -> ServiceSnapshot:
        return ServiceSnapshot(
            controller_active=self.is_controller_active(),
            codex_worker_active=self.is_codex_worker_active(),
        )

    def is_controller_active(self) -> bool:
        return self._active(_CONTROLLER_UNIT)

    def is_codex_worker_active(self) -> bool:
        return self._active(_CODEX_WORKER_UNIT)

    def restart_controller(self) -> None:
        self._action("restart", _CONTROLLER_UNIT)

    def stop_codex_worker(self) -> None:
        self._action("stop", _CODEX_WORKER_UNIT)

    def start_codex_worker(self) -> None:
        self._action("start", _CODEX_WORKER_UNIT)

    def restore(self, initial: ServiceSnapshot) -> None:
        failures: list[AcceptanceRuntimeError] = []
        for was_active, is_active, start in (
            (
                initial.controller_active,
                self.is_controller_active,
                lambda: self._action("start", _CONTROLLER_UNIT),
            ),
            (
                initial.codex_worker_active,
                self.is_codex_worker_active,
                lambda: self._action("start", _CODEX_WORKER_UNIT),
            ),
        ):
            if not was_active:
                continue
            try:
                if not is_active():
                    start()
            except AcceptanceRuntimeError as exc:
                failures.append(exc)
        if failures:
            raise AcceptanceRuntimeError(
                "service restoration failed for the fixed acceptance units"
            ) from failures[0]
