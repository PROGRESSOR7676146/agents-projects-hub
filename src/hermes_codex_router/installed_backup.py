"""Installed deployment description and native SQLite backup; no scheduler.

The private installation record is supplied by the owning release procedure.
Observed process paths never extend its accepted artifact set. This module does
not publish an active installation or grant installation/recovery authority.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import zipfile
from collections.abc import Callable
from contextlib import closing
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from .artifacts import artifact_spool_root
from .deployment_manifest import (
    ArtifactDescriptor,
    DeploymentManifest,
    DeploymentManifestError,
    _digest,
    _regular_file,
    verify_deployment_manifest,
)
from .hub_config import HubConfig, load_hub_config
from .incoming_materials import incoming_storage_root
from .installed_processes import observe_units
from .migrations import backup_database
from .runtime_health import (
    CONTROLLER_INSTANCE_ID,
    MONITOR_INSTANCE_ID,
    PROJECT_PROVISIONER_INSTANCE_ID,
    SENDER_INSTANCE_ID,
)
from .runtime_release import installed_runtime_digest
from .worker_identity import worker_instance_id

Observe = Callable[[tuple[str, ...]], dict[str, dict[str, Any]]]
_UNIT = re.compile(r"[A-Za-z0-9_.@-]+\.service")
_SHA256 = re.compile(r"[a-f0-9]{64}")


class InstalledBackupError(DeploymentManifestError):
    pass


def _require(condition: object, reason: str) -> None:
    if not condition:
        raise InstalledBackupError(reason)


def file_descriptor(path: Path) -> dict[str, str]:
    resolved = _regular_file(path)
    return {"path": str(resolved), "sha256": _digest(resolved)}


def _file(value: Any, label: str, *, private: bool = False) -> Path:
    _require(
        isinstance(value, dict) and set(value) == {"path", "sha256"},
        f"{label}: invalid file descriptor",
    )
    _require(
        isinstance(value["path"], str)
        and Path(value["path"]).is_absolute()
        and isinstance(value["sha256"], str)
        and _SHA256.fullmatch(value["sha256"]),
        f"{label}: invalid path or digest",
    )
    path = _regular_file(Path(value["path"]), private=private)
    _require(
        str(path) == value["path"] and _digest(path) == value["sha256"],
        f"{label}: file changed or is not canonical",
    )
    return path


def _json(path: Path) -> dict[str, Any]:
    _require(path.stat().st_size <= 1024 * 1024, "installed description is too large")

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            _require(key not in result, "installed description has duplicate fields")
            result[key] = value
        return result

    try:
        value = json.loads(path.read_text(), object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise InstalledBackupError("installed description is not valid JSON") from exc
    _require(isinstance(value, dict), "installed description must be an object")
    return value


def _load_config(value: Any) -> HubConfig:
    return load_hub_config(
        _file(value, "configuration", private=True),
        allow_unbound=True,
        _validate_telegram_secrets=False,
        _validate_project_provisioning_secret=False,
    )


def _topology(config: HubConfig) -> dict[tuple[str, str], bool]:
    expected = {
        ("controller", CONTROLLER_INSTANCE_ID): True,
        ("monitor", MONITOR_INSTANCE_ID): False,
    }
    if config.outbox_runtime == "external":
        expected[("sender", SENDER_INSTANCE_ID)] = True
    if config.project_provisioning.enabled:
        expected[("project_provisioner", PROJECT_PROVISIONER_INSTANCE_ID)] = True
    if config.dispatch_mode == "queue" and config.queue_runtime == "external":
        for agent in config.external_worker_agent_ids or ("codex",):
            for slot in range(1, config.worker_count_for_agent(agent) + 1):
                expected[("provider_worker", worker_instance_id(agent, slot))] = True
    return expected


def _databases(config: HubConfig) -> dict[str, Path]:
    result = {"shared": config.state_path}
    for agent in config.agents:
        if agent.runtime in {"gemini", "antigravity", "opencode"} and not agent.managed_externally:
            path = config.state_path.with_name(
                f"{config.state_path.stem}-{agent.agent_id}-dm{config.state_path.suffix}"
            )
            if path.exists():
                result[f"{agent.agent_id}-dm"] = path
    expected = set(result.values()) - {config.state_path}
    observed = set(
        config.state_path.parent.glob(f"{config.state_path.stem}-*-dm{config.state_path.suffix}")
    )
    _require(observed == expected, "unknown direct-message database consumer")
    return result


def _schema(path: Path) -> int:
    source = _regular_file(path, private=True)
    try:
        with closing(
            sqlite3.connect(f"file:{quote(str(source), safe='/')}?mode=ro", uri=True, timeout=1)
        ) as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:
        raise InstalledBackupError("installed database is busy or unreadable") from exc


def _database_info(path: Path) -> dict[str, Any]:
    before = path.stat()
    schema = _schema(path)
    after = path.stat()
    identity = [before.st_dev, before.st_ino]
    _require(
        identity == [after.st_dev, after.st_ino], "database replaced during inventory readback"
    )
    return {"path": str(path), "schema_version": schema, "file_identity": identity}


def _installed_package(root: Any, artifact: ArtifactDescriptor) -> None:
    _require(isinstance(root, str) and Path(root).is_absolute(), "invalid installed package root")
    package = Path(root)
    _require(
        package.resolve(strict=True) == package and package.name == "hermes_codex_router",
        "installed package root must be canonical",
    )
    names: set[Path] = set()
    with zipfile.ZipFile(artifact.path) as archive:
        for member in archive.infolist():
            relative = PurePosixPath(member.filename)
            if not relative.parts or relative.parts[0] != "hermes_codex_router" or member.is_dir():
                continue
            _require(
                ".." not in relative.parts and not relative.is_absolute(),
                "installed artifact has unsafe members",
            )
            path = package.joinpath(*relative.parts[1:])
            _require(path not in names, "installed artifact has duplicate members")
            names.add(path)
            _require(
                not path.is_symlink()
                and path.resolve(strict=True) == path
                and path.is_file()
                and path.read_bytes() == archive.read(member),
                "installed package differs from accepted artifact",
            )
    actual = {p for p in package.rglob("*") if p.is_file() and "__pycache__" not in p.parts}
    _require(actual == names, "installed package contains unaccepted artifact files")


def _process(
    record: dict[str, Any], observed: dict[str, Any] | None, required: bool
) -> dict[str, Any]:
    argv = record["argv"]
    _require(
        isinstance(argv, list)
        and bool(argv)
        and all(isinstance(arg, str) and arg and "\0" not in arg for arg in argv),
        "invalid installed process argv",
    )
    executable = _file(record["executable"], "process executable")
    if observed is None:
        _require(not required, "required installed consumer is not active")
        return {"status": "inactive"}
    _require(
        observed.get("argv") == argv
        and observed.get("executable") == str(executable)
        and isinstance(observed.get("start_marker"), str)
        and observed["start_marker"],
        "installed process identity drift",
    )
    return {"status": "active", "start_marker": observed["start_marker"]}


def _runtime(record: dict[str, Any], config: HubConfig, verified: set[tuple[str, str]]) -> None:
    pin = record["runtime"]
    _require(
        isinstance(pin, dict)
        and set(pin) == {"root", "sha256"}
        and isinstance(pin["root"], str)
        and isinstance(pin["sha256"], str),
        "invalid installed runtime pin",
    )
    root = Path(pin["root"])
    runtime_key = pin["root"], pin["sha256"]
    if runtime_key not in verified:
        _require(
            installed_runtime_digest(root) == pin["sha256"], "installed runtime digest changed"
        )
        verified.add(runtime_key)
    _require(isinstance(record["package_root"], str), "invalid installed package root")
    package = Path(record["package_root"])
    _require(
        package.parent.name == "site-packages"
        and package.parent.parent.name.startswith("python")
        and package.parent.parent.parent == root / "venv/lib",
        "installed package is not in its pinned runtime",
    )
    argv = record["argv"]
    python = str(root / "venv/bin/python")
    executable = _file(record["executable"], "process executable")
    _require(
        isinstance(argv, list) and argv and Path(python).resolve(strict=True) == executable,
        "process executable is not from the accepted runtime",
    )
    prefix = [python, "-I", "-B", "-m", "hermes_codex_router.cli"]
    _require(
        argv[: len(prefix)] == prefix,
        "unsupported installed launch; isolated module launch required",
    )
    command = {"provider_worker": "worker", "project_provisioner": "project-provisioner"}.get(
        record["component"], record["component"]
    )
    _require(
        argv[len(prefix) : len(prefix) + 2] == [command, record["configuration"]["path"]],
        "installed command/configuration binding differs",
    )
    if record["component"] == "provider_worker":
        tail = argv[len(prefix) + 2 :]
        agent_slots = [
            (agent, slot)
            for agent in config.external_worker_agent_ids or ("codex",)
            for slot in range(1, config.worker_count_for_agent(agent) + 1)
            if worker_instance_id(agent, slot) == record["instance_id"]
        ]
        _require(len(agent_slots) == 1, "worker does not exist in its installed configuration")
        agent, slot = agent_slots[0]
        _require(
            tail == ["--agent", agent, "--slot", str(slot)]
            or (slot == 1 and tail == ["--agent", agent]),
            "installed worker command does not match its slot",
        )


def _safe_record(record: dict[str, Any]) -> dict[str, Any]:
    # Private installed inventory may contain paths, never arbitrary process arguments.
    return {
        **{key: value for key, value in record.items() if key != "argv"},
        "argv_sha256": hashlib.sha256(json.dumps(record["argv"]).encode()).hexdigest(),
    }


def describe_installation(path: Path, *, observe: Observe = observe_units) -> dict[str, Any]:
    spec = _regular_file(path, private=True)
    spec_pin = file_descriptor(spec)
    document = _json(spec)
    version = document.get("schema_version")
    _require(
        document.get("kind") == "hub-installed-deployment",
        "expected hub-installed-deployment input; a preactivation manifest is not installed inventory",
    )
    _require(
        type(version) is int and version == 1,
        f"unsupported installed description version: {version}",
    )
    _require(
        set(document)
        == {
            "kind",
            "schema_version",
            "configuration",
            "components",
            "auxiliary",
            "recovery_artifacts",
        },
        "invalid installed description fields",
    )
    for field in ("components", "auxiliary", "recovery_artifacts"):
        _require(
            isinstance(document[field], list) and len(document[field]) <= 128,
            f"invalid installed {field}",
        )
    primary = _load_config(document["configuration"])
    _require(
        not primary.recovery_plane.enabled,
        "unsupported recovery-plane state consumer; owning compatibility adapter required",
    )
    _require(
        not any(
            agent.runtime in {"gemini", "antigravity", "opencode"} and not agent.managed_externally
            for agent in primary.agents
        ),
        "unsupported direct-provider state consumer; owning compatibility adapter required",
    )
    expected = _topology(primary)
    components = document["components"]
    auxiliary = document["auxiliary"]
    keys: set[tuple[str, str]] = set()
    units: set[str] = set()
    for record in [*components, *auxiliary]:
        _require(isinstance(record, dict), "invalid installed consumer record")
        unit = record.get("unit")
        _require(
            isinstance(unit, str) and _UNIT.fullmatch(unit) and unit not in units,
            "duplicate or invalid installed consumer unit",
        )
        units.add(unit)
    for record in components:
        _require(
            set(record)
            == {
                "component",
                "instance_id",
                "unit",
                "configuration",
                "release_manifest",
                "artifact",
                "package_root",
                "argv",
                "executable",
                "runtime",
            },
            "invalid Hub component fields",
        )
        _require(
            isinstance(record["component"], str) and isinstance(record["instance_id"], str),
            "invalid component identity",
        )
        key = record["component"], record["instance_id"]
        _require(key in expected and key not in keys, "unknown or duplicate configured consumer")
        keys.add(key)
    _require(keys == set(expected), "installed consumers do not match configured topology")
    observed = observe(tuple(sorted(units)))
    _require(not set(observed) - units, "unknown active installed consumer")
    databases = _databases(primary)
    database_info = {name: _database_info(p) for name, p in databases.items()}
    result_components = []
    releases: set[str] = set()
    manifests: dict[Path, DeploymentManifest] = {}
    verified_runtimes: set[tuple[str, str]] = set()
    verified_packages: set[tuple[str, str]] = set()
    for record in components:
        manifest_path = _file(record["release_manifest"], "release provenance", private=True)
        if manifest_path not in manifests:
            manifests[manifest_path] = verify_deployment_manifest(manifest_path)
        manifest = manifests[manifest_path]
        _require(
            isinstance(record["artifact"], str) and record["artifact"] in {"active", "rollback"},
            "unknown selected release artifact",
        )
        artifact = (
            manifest.active_artifact
            if record["artifact"] == "active"
            else manifest.rollback_artifact
        )
        config = _load_config(record["configuration"])
        _require(
            record["configuration"]["sha256"] == manifest.configuration.sha256,
            "installed configuration is not bound to accepted release provenance",
        )
        _require(
            config.state_path == primary.state_path
            and config.registry_path == primary.registry_path
            and _databases(config) == databases,
            "component configuration has incompatible topology",
        )
        _runtime(record, config, verified_runtimes)
        package_key = record["package_root"], artifact.sha256
        if package_key not in verified_packages:
            _installed_package(record["package_root"], artifact)
            verified_packages.add(package_key)
        _require(
            all(
                artifact.schema_min <= item["schema_version"] <= artifact.schema_max
                for item in database_info.values()
            ),
            "installed consumer cannot read retained schema",
        )
        key = record["component"], record["instance_id"]
        running = _process(record, observed.get(record["unit"]), expected[key])
        releases.add(artifact.git_sha)
        result_components.append(
            {
                **_safe_record(record),
                "release": asdict(artifact),
                "running": running,
                "required": expected[key],
            }
        )
    result_auxiliary = []
    for record in auxiliary:
        _require(
            set(record) == {"unit", "owner", "required", "uses_hub_state", "argv", "executable"}
            and isinstance(record["owner"], str)
            and record["owner"] in {"codex/tlive", "cpa", "hermes"}
            and type(record["required"]) is bool
            and record["uses_hub_state"] is False,
            "unsupported auxiliary consumer compatibility contract",
        )
        running = _process(record, observed.get(record["unit"]), record["required"])
        result_auxiliary.append({**_safe_record(record), "running": running})
    for artifact in document["recovery_artifacts"]:
        _file(artifact, "recovery artifact")
    _file(spec_pin, "installed description", private=True)
    return {
        "kind": "hub-installed-description",
        "schema_version": 1,
        "installation": spec_pin,
        "configuration": document["configuration"],
        "registry": file_descriptor(primary.registry_path),
        "databases": database_info,
        "components": result_components,
        "auxiliary": result_auxiliary,
        "recovery_artifacts": document["recovery_artifacts"],
        "revision_status": "mixed" if len(releases) > 1 else "converged",
        "backup_compatible": True,
        "runtime_acceptance": False,
        "uncaptured_recovery_dependencies": [
            str(dependency)
            for p in databases.values()
            for dependency in (artifact_spool_root(p), incoming_storage_root(p))
        ],
    }


def _write(path: Path, value: Any) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def backup_installation(
    path: Path,
    destination: Path,
    *,
    observe: Observe = observe_units,
    state_path: Path | None = None,
) -> dict[str, Any]:
    before = describe_installation(path, observe=observe)
    if state_path is not None:
        _require(
            str(state_path.resolve(strict=True)) == before["databases"]["shared"]["path"],
            "backup state differs from installed description",
        )
    output = destination.absolute()
    _require(output.resolve() == output, "snapshot destination must be canonical")
    try:
        output.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise InstalledBackupError(
            "snapshot already exists; inspect PENDING.json and receipt.json"
        ) from exc
    _write(output / "PENDING.json", {"status": "pending", "installation": before})
    snapshots = {}
    for name, database in before["databases"].items():
        target = backup_database(
            Path(database["path"]), output / f"{name}.db", timeout_seconds=30, read_only_source=True
        )
        _require(
            _schema(target) == database["schema_version"], "schema changed during installed backup"
        )
        snapshots[name] = file_descriptor(target)
    after = describe_installation(path, observe=observe)
    _require(
        before == after, "installation changed during backup; inspect retained pending snapshot"
    )
    receipt = {
        "status": "complete",
        "schema_version": 1,
        "installation": before,
        "databases": snapshots,
        "captured_scope": "sqlite-only",
        "complete_recovery_set": False,
    }
    _write(output / "receipt.json", receipt)
    descriptor = os.open(output, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return receipt


def inspect_backup(destination: Path) -> dict[str, Any]:
    """Inspect a lost-ACK outcome without current installation access or replay."""
    output = destination.resolve(strict=True)
    _require(
        output.is_dir() and not output.stat().st_mode & 0o077, "snapshot directory must be private"
    )
    pending = _json(_regular_file(output / "PENDING.json", private=True))
    _require(
        pending.get("status") == "pending" and isinstance(pending.get("installation"), dict),
        "original pending operation is incomplete",
    )
    receipt_path = output / "receipt.json"
    if not receipt_path.exists():
        return {"status": "pending", "installation": pending["installation"]}
    receipt = _json(_regular_file(receipt_path, private=True))
    _require(
        receipt.get("status") == "complete"
        and type(receipt.get("schema_version")) is int
        and receipt.get("schema_version") == 1
        and receipt.get("installation") == pending.get("installation"),
        "snapshot receipt is incomplete or conflicts with original pending operation",
    )
    _require(
        isinstance(receipt["installation"].get("databases"), dict)
        and isinstance(receipt.get("databases"), dict)
        and set(receipt["databases"]) == set(receipt["installation"]["databases"]),
        "snapshot database set is incomplete",
    )
    for value in receipt["databases"].values():
        snapshot = _file(value, "snapshot database", private=True)
        _require(snapshot.parent == output, "snapshot database escapes original operation")
    return receipt
