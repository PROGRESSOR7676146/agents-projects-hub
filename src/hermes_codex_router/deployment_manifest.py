from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import sqlite3
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

_SHA256 = re.compile(r"[0-9a-f]{64}")
_GIT_SHA = re.compile(r"[0-9a-f]{40,64}")
_SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_MAX_MANIFEST_BYTES = 64 * 1024


class DeploymentManifestError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    path: str
    sha256: str
    package_version: str
    git_sha: str
    built_at: str
    clean_tree: bool
    schema_min: int
    schema_max: int


@dataclass(frozen=True, slots=True)
class FileDescriptor:
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class StateBackupDescriptor:
    path: str
    sha256: str
    schema_version: int


@dataclass(frozen=True, slots=True)
class DeploymentManifest:
    schema_version: int
    created_at: str
    target_schema_version: int
    active_artifact: ArtifactDescriptor
    rollback_artifact: ArtifactDescriptor
    configuration: FileDescriptor
    state_backup: StateBackupDescriptor


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_file(path: Path, *, private: bool = False) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise DeploymentManifestError("release input must be a regular file")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise DeploymentManifestError("release input must be a regular file")
    if private and resolved.stat().st_mode & 0o077:
        raise DeploymentManifestError("private release input must use mode 0600")
    return resolved


def _literal_assignments(source: str, *, names: set[str]) -> dict[str, object]:
    try:
        module = ast.parse(source)
    except SyntaxError as exc:
        raise DeploymentManifestError("artifact metadata is not valid Python") from exc
    values: dict[str, object] = {}
    for statement in module.body:
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        target = statement.targets[0] if isinstance(statement, ast.Assign) else statement.target
        value = statement.value
        if isinstance(target, ast.Name) and target.id in names and value is not None:
            try:
                values[target.id] = ast.literal_eval(value)
            except (ValueError, TypeError) as exc:
                raise DeploymentManifestError("artifact metadata must use literals") from exc
    if set(values) != names:
        raise DeploymentManifestError("artifact metadata is incomplete")
    return values


def inspect_wheel(path: Path) -> ArtifactDescriptor:
    artifact = _regular_file(path)
    if artifact.suffix != ".whl":
        raise DeploymentManifestError("release artifact must be a wheel")
    try:
        with zipfile.ZipFile(artifact) as archive:
            build_info = _literal_assignments(
                archive.read("hermes_codex_router/_build_info.py").decode("utf-8"),
                names={"PACKAGE_VERSION", "GIT_SHA", "BUILT_AT", "CLEAN_TREE"},
            )
            compatibility = _literal_assignments(
                archive.read("hermes_codex_router/schema_compatibility.py").decode("utf-8"),
                names={"MIN_SUPPORTED_SCHEMA_VERSION", "MAX_SUPPORTED_SCHEMA_VERSION"},
            )
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                raise DeploymentManifestError("artifact must contain one wheel METADATA file")
            metadata = archive.read(metadata_names[0]).decode("utf-8")
    except (KeyError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise DeploymentManifestError("release artifact is not an inspectable Hub wheel") from exc

    metadata_versions = [
        line.removeprefix("Version: ").strip()
        for line in metadata.splitlines()
        if line.startswith("Version: ")
    ]
    package_version = build_info["PACKAGE_VERSION"]
    git_sha = build_info["GIT_SHA"]
    built_at = build_info["BUILT_AT"]
    clean_tree = build_info["CLEAN_TREE"]
    schema_min = compatibility["MIN_SUPPORTED_SCHEMA_VERSION"]
    schema_max = compatibility["MAX_SUPPORTED_SCHEMA_VERSION"]
    if not isinstance(package_version, str) or _SEMVER.fullmatch(package_version) is None:
        raise DeploymentManifestError("artifact package version is invalid")
    if metadata_versions != [package_version]:
        raise DeploymentManifestError("wheel metadata and embedded package version differ")
    if not isinstance(git_sha, str) or _GIT_SHA.fullmatch(git_sha) is None:
        raise DeploymentManifestError("artifact Git SHA is invalid")
    if not isinstance(built_at, str):
        raise DeploymentManifestError("artifact build time is invalid")
    try:
        parsed_build_time = datetime.fromisoformat(built_at)
    except ValueError as exc:
        raise DeploymentManifestError("artifact build time is invalid") from exc
    if parsed_build_time.tzinfo is None:
        raise DeploymentManifestError("artifact build time must be timezone-aware")
    if clean_tree is not True:
        raise DeploymentManifestError("artifact is not a clean-tree immutable release")
    if (
        not isinstance(schema_min, int)
        or isinstance(schema_min, bool)
        or not isinstance(schema_max, int)
        or isinstance(schema_max, bool)
        or schema_min < 1
        or schema_max < schema_min
    ):
        raise DeploymentManifestError("artifact schema compatibility range is invalid")
    return ArtifactDescriptor(
        str(artifact),
        _digest(artifact),
        package_version,
        git_sha,
        built_at,
        clean_tree,
        schema_min,
        schema_max,
    )


def inspect_database(path: Path) -> tuple[Path, str, int]:
    database = _regular_file(path, private=True)
    uri = f"file:{quote(str(database), safe='/')}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True)
        try:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            check = connection.execute("PRAGMA integrity_check").fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise DeploymentManifestError("state backup is not a readable SQLite database") from exc
    if check is None or check[0] != "ok":
        raise DeploymentManifestError("state backup integrity check failed")
    if version < 1:
        raise DeploymentManifestError("state backup schema version is not initialized")
    return database, _digest(database), version


def _supports(artifact: ArtifactDescriptor, schema_version: int) -> bool:
    return artifact.schema_min <= schema_version <= artifact.schema_max


def _validate_compatibility(manifest: DeploymentManifest) -> None:
    active = manifest.active_artifact
    rollback = manifest.rollback_artifact
    backup_schema = manifest.state_backup.schema_version
    target_schema = manifest.target_schema_version
    if active.sha256 == rollback.sha256 or active.git_sha == rollback.git_sha:
        raise DeploymentManifestError("active and rollback artifacts must be distinct releases")
    if target_schema != active.schema_max:
        raise DeploymentManifestError("target schema must equal the active artifact target")
    if backup_schema > target_schema or not _supports(active, backup_schema):
        raise DeploymentManifestError("active artifact cannot migrate the backup schema")
    if not _supports(active, target_schema):
        raise DeploymentManifestError("active artifact cannot open its target schema")
    if not _supports(rollback, target_schema):
        raise DeploymentManifestError("rollback artifact cannot open the target schema")


def create_deployment_manifest(
    output: Path,
    *,
    active_artifact: Path,
    rollback_artifact: Path,
    configuration: Path,
    state_backup: Path,
    created_at: datetime | None = None,
) -> DeploymentManifest:
    active = inspect_wheel(active_artifact)
    rollback = inspect_wheel(rollback_artifact)
    config = _regular_file(configuration, private=True)
    backup, backup_digest, backup_schema = inspect_database(state_backup)
    observed_at = created_at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise DeploymentManifestError("manifest creation time must be timezone-aware")
    manifest = DeploymentManifest(
        1,
        observed_at.isoformat(),
        active.schema_max,
        active,
        rollback,
        FileDescriptor(str(config), _digest(config)),
        StateBackupDescriptor(str(backup), backup_digest, backup_schema),
    )
    _validate_compatibility(manifest)
    destination = output.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise DeploymentManifestError("deployment manifest already exists") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(asdict(manifest), stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return manifest


def _exact_keys(value: dict[str, Any], expected: set[str], *, label: str) -> None:
    if set(value) != expected:
        raise DeploymentManifestError(f"{label} fields are invalid")


def _parse_artifact(value: object, *, label: str) -> ArtifactDescriptor:
    if not isinstance(value, dict):
        raise DeploymentManifestError(f"{label} must be an object")
    _exact_keys(
        value,
        {
            "path",
            "sha256",
            "package_version",
            "git_sha",
            "built_at",
            "clean_tree",
            "schema_min",
            "schema_max",
        },
        label=label,
    )
    try:
        descriptor = ArtifactDescriptor(**value)
    except TypeError as exc:
        raise DeploymentManifestError(f"{label} fields are invalid") from exc
    if not isinstance(descriptor.path, str) or not isinstance(descriptor.sha256, str):
        raise DeploymentManifestError(f"{label} fields are invalid")
    return descriptor


def load_deployment_manifest(path: Path) -> DeploymentManifest:
    manifest_path = _regular_file(path, private=True)
    if manifest_path.stat().st_size > _MAX_MANIFEST_BYTES:
        raise DeploymentManifestError("deployment manifest is too large")
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeploymentManifestError("deployment manifest is not valid JSON") from exc
    if not isinstance(document, dict):
        raise DeploymentManifestError("deployment manifest must be an object")
    _exact_keys(
        document,
        {
            "schema_version",
            "created_at",
            "target_schema_version",
            "active_artifact",
            "rollback_artifact",
            "configuration",
            "state_backup",
        },
        label="deployment manifest",
    )
    configuration = document["configuration"]
    state_backup = document["state_backup"]
    if not isinstance(configuration, dict) or not isinstance(state_backup, dict):
        raise DeploymentManifestError("deployment manifest file descriptors are invalid")
    _exact_keys(configuration, {"path", "sha256"}, label="configuration")
    _exact_keys(state_backup, {"path", "sha256", "schema_version"}, label="state backup")
    try:
        manifest = DeploymentManifest(
            schema_version=document["schema_version"],
            created_at=document["created_at"],
            target_schema_version=document["target_schema_version"],
            active_artifact=_parse_artifact(document["active_artifact"], label="active artifact"),
            rollback_artifact=_parse_artifact(
                document["rollback_artifact"], label="rollback artifact"
            ),
            configuration=FileDescriptor(**configuration),
            state_backup=StateBackupDescriptor(**state_backup),
        )
    except TypeError as exc:
        raise DeploymentManifestError("deployment manifest fields are invalid") from exc
    if manifest.schema_version != 1:
        raise DeploymentManifestError("unsupported deployment manifest schema")
    if (
        not isinstance(manifest.target_schema_version, int)
        or isinstance(manifest.target_schema_version, bool)
        or manifest.target_schema_version < 1
        or not isinstance(manifest.configuration.path, str)
        or not isinstance(manifest.configuration.sha256, str)
        or _SHA256.fullmatch(manifest.configuration.sha256) is None
        or not isinstance(manifest.state_backup.path, str)
        or not isinstance(manifest.state_backup.sha256, str)
        or _SHA256.fullmatch(manifest.state_backup.sha256) is None
        or not isinstance(manifest.state_backup.schema_version, int)
        or isinstance(manifest.state_backup.schema_version, bool)
        or manifest.state_backup.schema_version < 1
    ):
        raise DeploymentManifestError("deployment manifest fields are invalid")
    if not isinstance(manifest.created_at, str):
        raise DeploymentManifestError("manifest creation time is invalid")
    try:
        created_at = datetime.fromisoformat(manifest.created_at)
    except ValueError as exc:
        raise DeploymentManifestError("manifest creation time is invalid") from exc
    if created_at.tzinfo is None:
        raise DeploymentManifestError("manifest creation time must be timezone-aware")
    return manifest


def verify_deployment_manifest(path: Path, *, state_path: Path | None = None) -> DeploymentManifest:
    manifest = load_deployment_manifest(path)
    active = inspect_wheel(Path(manifest.active_artifact.path))
    rollback = inspect_wheel(Path(manifest.rollback_artifact.path))
    if active != manifest.active_artifact:
        raise DeploymentManifestError("active artifact does not match deployment manifest")
    if rollback != manifest.rollback_artifact:
        raise DeploymentManifestError("rollback artifact does not match deployment manifest")
    config = _regular_file(Path(manifest.configuration.path), private=True)
    if _digest(config) != manifest.configuration.sha256:
        raise DeploymentManifestError("configuration does not match deployment manifest")
    backup, backup_digest, backup_schema = inspect_database(Path(manifest.state_backup.path))
    if (
        str(backup) != manifest.state_backup.path
        or backup_digest != manifest.state_backup.sha256
        or backup_schema != manifest.state_backup.schema_version
    ):
        raise DeploymentManifestError("state backup does not match deployment manifest")
    _validate_compatibility(manifest)
    if state_path is not None:
        _, _, state_schema = inspect_database(state_path)
        if state_schema != manifest.target_schema_version:
            raise DeploymentManifestError("active state schema does not match deployment target")
        if not _supports(active, state_schema) or not _supports(rollback, state_schema):
            raise DeploymentManifestError("active state schema is not rollback-compatible")
    return manifest
