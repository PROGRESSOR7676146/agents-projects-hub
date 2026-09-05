from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from .deployment_manifest import (
    ArtifactDescriptor,
    create_deployment_manifest,
    inspect_wheel,
    verify_deployment_manifest,
)
from .migrations import backup_database, migrate_database


class ReleaseDryRunError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseDryRunReport:
    active_release: ArtifactDescriptor
    rollback_release: ArtifactDescriptor
    schema_before: int
    schema_after_rollout: int
    schema_after_rollback: int
    durable_work_preserved: bool
    rollback_pointer_restored: bool
    manifest_verified: bool
    temporary_state_only: bool = True
    service_actions: bool = False
    network_actions: bool = False


def _seed_production_shaped_v20(path: Path) -> None:
    migrate_database(path, create_backup=False)
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP INDEX runtime_events_retention")
        connection.execute(
            "CREATE INDEX runtime_events_created_at ON runtime_events(created_at DESC)"
        )
        connection.executescript(
            """
            INSERT INTO topics
                (project_id, chat_id, thread_id, title, active_agent_id, created_at, updated_at)
            VALUES ('example-project', -1001234567890, 7, 'Release dry run', 'codex',
                    '2026-09-05T10:00:00+00:00', '2026-09-05T10:00:00+00:00');
            INSERT INTO agent_sessions
                (session_id, topic_id, agent_id, generation, status, model, effort,
                 provider_session_id, writer_mode, created_at, updated_at)
            VALUES ('session', 1, 'codex', 1, 'active', 'example-model', 'high',
                    'provider-session', 'telegram', '2026-09-05T10:00:00+00:00',
                    '2026-09-05T10:00:00+00:00');
            INSERT INTO provider_jobs
                (job_id, idempotency_key, chat_id, message_id, topic_id, topic_sequence,
                 agent_id, session_id, session_generation, provider_session_id, model, effort,
                 payload_text, status, attempt_count, provider_started_at, error_class,
                 error_code, error_detail, created_at, updated_at)
            VALUES
                ('queued-job', 'queued-key', -1001234567890, 101, 1, 1, 'codex', 'session',
                 1, 'provider-session', 'example-model', 'high', 'queued payload', 'queued',
                 0, NULL, NULL, NULL, NULL, '2026-09-05T10:01:00+00:00',
                 '2026-09-05T10:01:00+00:00'),
                ('result-job', 'result-key', -1001234567890, 102, 1, 2, 'codex', 'session',
                 1, 'provider-session', 'example-model', 'high', 'result payload',
                 'result_ready', 1, '2026-09-05T10:02:00+00:00', NULL, NULL, NULL,
                 '2026-09-05T10:02:00+00:00', '2026-09-05T10:02:00+00:00'),
                ('indeterminate-job', 'indeterminate-key', -1001234567890, 103, 1, 3,
                 'codex', 'session', 1, 'provider-session', 'example-model', 'high',
                 'ambiguous payload', 'indeterminate', 1, '2026-09-05T10:03:00+00:00',
                 'indeterminate', 'provider_outcome_unknown', 'acceptance not proven',
                 '2026-09-05T10:03:00+00:00', '2026-09-05T10:03:00+00:00');
            INSERT INTO provider_job_results
                (result_id, job_id, visible_response, provider_session_id, actual_model, created_at)
            VALUES ('result', 'result-job', 'durable response', 'provider-session',
                    'example-model', '2026-09-05T10:04:00+00:00');
            INSERT INTO telegram_outbox
                (outbox_id, job_id, sender_agent_id, chat_id, thread_id, telegram_html,
                 status, attempt_count, available_at, created_at, updated_at)
            VALUES ('outbox', 'result-job', 'codex', -1001234567890, 7, 'durable response',
                    'pending', 2, '2026-09-05T10:04:00+00:00',
                    '2026-09-05T10:04:00+00:00', '2026-09-05T10:04:00+00:00');
            INSERT INTO telegram_outbox_parts (outbox_id, part_index, telegram_html, part_type)
            VALUES ('outbox', 1, 'durable response', 'text');
            INSERT INTO runtime_events (component, level, code, detail, created_at)
            VALUES ('controller', 'info', 'expired', 'retention candidate',
                    '2020-01-01T00:00:00+00:00');
            PRAGMA user_version = 20;
            """
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(path, 0o600)


def _state_snapshot(path: Path) -> tuple[int, tuple[tuple[object, ...], ...]]:
    connection = sqlite3.connect(path)
    try:
        schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
        work = tuple(
            connection.execute(
                """SELECT provider_jobs.job_id, provider_jobs.status,
                          provider_jobs.attempt_count, provider_jobs.error_class,
                          provider_jobs.error_code, telegram_outbox.status,
                          telegram_outbox.attempt_count
                   FROM provider_jobs
                   LEFT JOIN telegram_outbox USING (job_id)
                   ORDER BY provider_jobs.topic_sequence"""
            ).fetchall()
        )
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ReleaseDryRunError("temporary state integrity check failed")
        return schema, work
    finally:
        connection.close()


def _extract_wheel(artifact: Path, destination: Path) -> None:
    destination.mkdir(mode=0o755)
    root = destination.resolve()
    with zipfile.ZipFile(artifact) as archive:
        for info in archive.infolist():
            member = (destination / info.filename).resolve()
            try:
                member.relative_to(root)
            except ValueError as exc:
                raise ReleaseDryRunError("wheel member escapes release directory") from exc
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ReleaseDryRunError("wheel contains a symbolic link")
            if info.is_dir():
                member.mkdir(parents=True, exist_ok=True)
                continue
            member.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, member.open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
            os.chmod(member, 0o444)


def _switch_pointer(pointer: Path, target: Path) -> None:
    replacement = pointer.with_name(f".{pointer.name}.next")
    replacement.unlink(missing_ok=True)
    replacement.symlink_to(target)
    os.replace(replacement, pointer)


def _run_artifact_migration(release_root: Path, state_path: Path) -> dict[str, object]:
    script = """
import json
import sqlite3
import sys
from pathlib import Path
from hermes_codex_router._build_info import BUILT_AT, CLEAN_TREE, GIT_SHA, PACKAGE_VERSION
from hermes_codex_router.migrations import migrate_database
from hermes_codex_router.schema_compatibility import (
    MAX_SUPPORTED_SCHEMA_VERSION,
    MIN_SUPPORTED_SCHEMA_VERSION,
)

state = Path(sys.argv[1])
migrate_database(state, create_backup=False)
connection = sqlite3.connect(state)
try:
    schema = int(connection.execute("PRAGMA user_version").fetchone()[0])
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
finally:
    connection.close()
print(json.dumps({
    "package_version": PACKAGE_VERSION,
    "git_sha": GIT_SHA,
    "built_at": BUILT_AT,
    "clean_tree": CLEAN_TREE,
    "schema_min": MIN_SUPPORTED_SCHEMA_VERSION,
    "schema_max": MAX_SUPPORTED_SCHEMA_VERSION,
    "state_schema": schema,
    "integrity": integrity,
}))
"""
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(release_root),
    }
    completed = subprocess.run(
        (sys.executable, "-c", script, str(state_path)),
        cwd=release_root.parent,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise ReleaseDryRunError("artifact failed the temporary state-open gate")
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseDryRunError("artifact state-open gate returned invalid output") from exc
    if not isinstance(result, dict) or result.get("integrity") != "ok":
        raise ReleaseDryRunError("artifact state-open gate failed integrity verification")
    return result


def run_release_dry_run(active_artifact: Path, rollback_artifact: Path) -> ReleaseDryRunReport:
    active = inspect_wheel(active_artifact)
    rollback = inspect_wheel(rollback_artifact)
    with tempfile.TemporaryDirectory(prefix="agents-projects-hub-release-dry-run-") as directory:
        root = Path(directory)
        state_path = root / "state.db"
        backup_path = root / "state-v20.backup"
        config_path = root / "hub.json"
        manifest_path = root / "deployment-manifest.json"
        releases = root / "releases"
        rollback_root = releases / rollback.git_sha
        active_root = releases / active.git_sha
        pointer = root / "active"

        _seed_production_shaped_v20(state_path)
        schema_before, expected_work = _state_snapshot(state_path)
        backup_database(state_path, backup_path)
        config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "dry_run": True,
                    "state": "temporary-only",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(config_path, 0o600)
        create_deployment_manifest(
            manifest_path,
            active_artifact=active_artifact,
            rollback_artifact=rollback_artifact,
            configuration=config_path,
            state_backup=backup_path,
        )
        verify_deployment_manifest(manifest_path)

        releases.mkdir(mode=0o755)
        _extract_wheel(rollback_artifact, rollback_root)
        _extract_wheel(active_artifact, active_root)
        _switch_pointer(pointer, rollback_root)
        _switch_pointer(pointer, active_root)
        active_result = _run_artifact_migration(pointer.resolve(), state_path)
        schema_after_rollout, rollout_work = _state_snapshot(state_path)
        verify_deployment_manifest(manifest_path, state_path=state_path)

        _switch_pointer(pointer, rollback_root)
        rollback_result = _run_artifact_migration(pointer.resolve(), state_path)
        schema_after_rollback, rollback_work = _state_snapshot(state_path)
        pointer_restored = pointer.resolve() == rollback_root.resolve()

        if active_result.get("git_sha") != active.git_sha:
            raise ReleaseDryRunError(
                "temporary activation loaded the wrong candidate release: "
                f"expected {active.git_sha}, got {active_result.get('git_sha')}"
            )
        if rollback_result.get("git_sha") != rollback.git_sha:
            raise ReleaseDryRunError("temporary rollback loaded the wrong rollback release")
        durable_work_preserved = expected_work == rollout_work == rollback_work
        if not durable_work_preserved:
            raise ReleaseDryRunError("temporary rollout changed durable provider work")
        if schema_before != 20 or schema_after_rollout != 21 or schema_after_rollback != 21:
            raise ReleaseDryRunError("temporary rollout produced an unexpected schema transition")
        if not pointer_restored:
            raise ReleaseDryRunError("temporary activation pointer did not return to rollback")

        return ReleaseDryRunReport(
            active,
            rollback,
            schema_before,
            schema_after_rollout,
            schema_after_rollback,
            durable_work_preserved,
            pointer_restored,
            True,
        )


def report_dict(report: ReleaseDryRunReport) -> dict[str, object]:
    return {"ok": True, **asdict(report)}
