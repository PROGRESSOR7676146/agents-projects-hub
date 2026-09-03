from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path

MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024
MAX_JOB_ATTACHMENT_BYTES = 50 * 1024 * 1024
_COPY_CHUNK_BYTES = 1024 * 1024
_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_SENSITIVE_NAME = re.compile(
    r"(?:^|[._ -])(?:api[_ -]?key|private[_ -]?key|secret|token|password|"
    r"credential|id_rsa|id_ed25519|id_ecdsa|id_dsa)(?:[._ -]|$)",
    re.IGNORECASE,
)

FORBIDDEN_EXTENSIONS = frozenset(
    {
        ".pem",
        ".key",
        ".pfx",
        ".p12",
        ".kdbx",
        ".sqlite",
        ".db",
        ".log",
        ".env",
        ".sh",
        ".pyc",
        ".exe",
        ".dll",
        ".so",
        ".zip",
        ".tar.gz",
        ".tgz",
    }
)

ALLOWED_EXTENSIONS = frozenset(
    {
        ".md",
        ".txt",
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".svg",
        ".json",
        ".yaml",
        ".yml",
        ".csv",
        ".tsv",
        ".xml",
        ".html",
        ".diff",
        ".patch",
        ".drawio",
        ".puml",
        ".mermaid",
    }
)


class ArtifactSecurityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedArtifact:
    path: Path
    name: str
    size: int
    mime_type: str
    sha256: str = ""


def artifact_spool_root(state_path: Path) -> Path:
    """Return the Hub-owned root used for immutable queued attachments."""
    return state_path.expanduser().resolve().parent / "artifact-spool"


def create_job_staging(project_root: Path, *, prefix: str = "direct") -> tuple[str, Path]:
    """Create one private, collision-resistant staging directory for a provider turn."""
    canonical_root = project_root.expanduser().resolve(strict=True)
    job_id = f"{prefix}-{uuid.uuid4().hex}"
    staging = canonical_root / ".hub" / "staging" / job_id
    staging.mkdir(parents=True, exist_ok=False, mode=0o700)
    staging.chmod(0o700)
    return job_id, staging


def _get_extension(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".tar.gz"):
        return ".tar.gz"
    return path.suffix.lower()


def _validate_display_name(name: str) -> None:
    if not name or name in {".", ".."} or Path(name).name != name:
        raise ArtifactSecurityError("artifact filename is invalid")
    if name.startswith("."):
        raise ArtifactSecurityError(f"hidden files cannot be attached: {name}")
    if any(ord(character) < 32 or ord(character) == 127 for character in name):
        raise ArtifactSecurityError("artifact filename contains control characters")
    if '"' in name or "\\" in name:
        raise ArtifactSecurityError("artifact filename contains unsafe punctuation")
    if _SENSITIVE_NAME.search(name):
        raise ArtifactSecurityError(f"artifact name appears sensitive: {name}")


def validate_artifact_path(file_path: Path, project_root: Path) -> ValidatedArtifact:
    """Validate a candidate before copying it into the private delivery spool."""
    try:
        canonical = file_path.expanduser().resolve(strict=True)
        canonical_root = project_root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ArtifactSecurityError(f"artifact path could not be resolved: {file_path}") from exc

    if not canonical.is_relative_to(canonical_root):
        raise ArtifactSecurityError(f"artifact path escapes project root: {file_path}")
    if file_path.is_symlink() or not canonical.is_file() or canonical.is_symlink():
        raise ArtifactSecurityError(f"artifact must be a regular non-symlink file: {file_path}")

    _validate_display_name(canonical.name)
    extension = _get_extension(canonical)
    if extension in FORBIDDEN_EXTENSIONS or extension not in ALLOWED_EXTENSIONS:
        raise ArtifactSecurityError(f"artifact extension is not allowed: {extension}")

    size = canonical.stat().st_size
    if size <= 0:
        raise ArtifactSecurityError(f"artifact file is empty: {file_path}")
    if size > MAX_ATTACHMENT_BYTES:
        raise ArtifactSecurityError(
            f"artifact file exceeds size limit ({size} > {MAX_ATTACHMENT_BYTES}): {file_path}"
        )

    mime_type, _ = mimetypes.guess_type(canonical.name)
    return ValidatedArtifact(
        path=canonical,
        name=canonical.name,
        size=size,
        mime_type=mime_type or "application/octet-stream",
    )


def collect_staged_artifacts(
    project_root: Path,
    job_id: str,
    *,
    max_total_bytes: int = MAX_JOB_ATTACHMENT_BYTES,
    rejection_sink: list[str] | None = None,
) -> tuple[ValidatedArtifact, ...]:
    """Collect only the current job's staging directory; never reuse shared files."""
    if not _SAFE_JOB_ID.fullmatch(job_id) or not 0 <= max_total_bytes <= MAX_JOB_ATTACHMENT_BYTES:
        return ()
    try:
        canonical_root = project_root.expanduser().resolve(strict=True)
        staging = canonical_root / ".hub" / "staging" / job_id
        if not staging.is_dir() or staging.is_symlink():
            return ()
        discovered = sorted(staging.iterdir(), key=lambda item: item.name)
    except OSError:
        return ()

    validated: list[ValidatedArtifact] = []
    total_bytes = 0
    for candidate in discovered:
        try:
            artifact = validate_artifact_path(candidate, canonical_root)
        except ArtifactSecurityError as exc:
            if rejection_sink is not None:
                safe_name = candidate.name.encode("unicode_escape").decode("ascii")[:120]
                rejection_sink.append(f"{safe_name}: {exc}")
            continue
        if total_bytes + artifact.size > max_total_bytes:
            if rejection_sink is not None:
                rejection_sink.append(f"{artifact.name}: total attachment size limit exceeded")
            continue
        validated.append(artifact)
        total_bytes += artifact.size
    return tuple(validated)


def _private_directory(path: Path) -> None:
    if path.is_symlink():
        raise ArtifactSecurityError("artifact spool directory cannot be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def _copy_to_spool(source: ValidatedArtifact, destination: Path) -> ValidatedArtifact:
    source_fd = os.open(source.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_size != source.size:
            raise ArtifactSecurityError("artifact source changed before spooling")
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest = hashlib.sha256()
        copied = 0
        try:
            while chunk := os.read(source_fd, _COPY_CHUNK_BYTES):
                copied += len(chunk)
                if copied > MAX_ATTACHMENT_BYTES:
                    raise ArtifactSecurityError("artifact exceeded size limit while spooling")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(destination_fd, view) :]
            os.fsync(destination_fd)
        except BaseException:
            os.close(destination_fd)
            destination.unlink(missing_ok=True)
            raise
        else:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
    if copied != source.size:
        destination.unlink(missing_ok=True)
        raise ArtifactSecurityError("artifact source changed while spooling")
    return ValidatedArtifact(destination, source.name, copied, source.mime_type, digest.hexdigest())


def spool_staged_artifacts(
    project_root: Path,
    job_id: str,
    spool_root: Path,
    *,
    max_total_bytes: int = MAX_JOB_ATTACHMENT_BYTES,
    rejection_sink: list[str] | None = None,
) -> tuple[ValidatedArtifact, ...]:
    """Snapshot validated job artifacts into a private immutable Hub-owned spool."""
    candidates = collect_staged_artifacts(
        project_root,
        job_id,
        max_total_bytes=max_total_bytes,
        rejection_sink=rejection_sink,
    )
    if not candidates:
        return ()
    canonical_spool = spool_root.expanduser().resolve()
    _private_directory(canonical_spool)
    job_spool = canonical_spool / job_id
    _private_directory(job_spool)
    snapshots: list[ValidatedArtifact] = []
    try:
        for source in candidates:
            suffix = "".join(Path(source.name).suffixes)[-32:]
            snapshots.append(_copy_to_spool(source, job_spool / f"{uuid.uuid4().hex}{suffix}"))
    except BaseException:
        for artifact in snapshots:
            artifact.path.unlink(missing_ok=True)
        try:
            job_spool.rmdir()
        except OSError:
            pass
        raise
    return tuple(snapshots)


def verify_spooled_artifact(
    file_path: Path,
    spool_root: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> None:
    """Revalidate an immutable spool object immediately before Telegram delivery."""
    try:
        canonical_root = spool_root.expanduser().resolve(strict=True)
        canonical = file_path.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ArtifactSecurityError("spooled artifact is missing") from exc
    if not canonical.is_relative_to(canonical_root) or file_path.is_symlink():
        raise ArtifactSecurityError("spooled artifact escaped its private root")
    descriptor = os.open(canonical, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
            raise ArtifactSecurityError("spooled artifact metadata changed")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, _COPY_CHUNK_BYTES):
            digest.update(chunk)
    finally:
        os.close(descriptor)
    if not expected_sha256 or digest.hexdigest() != expected_sha256:
        raise ArtifactSecurityError("spooled artifact digest changed")


def remove_spooled_artifact(file_path: Path, spool_root: Path) -> None:
    """Delete one confirmed-delivered spool object without following replacements."""
    canonical_root = spool_root.expanduser().resolve(strict=True)
    parent = file_path.parent.resolve(strict=True)
    if not parent.is_relative_to(canonical_root) or file_path.is_symlink():
        raise ArtifactSecurityError("refusing to remove an untrusted artifact path")
    file_path.unlink(missing_ok=True)
    try:
        parent.rmdir()
    except OSError:
        pass


def cleanup_job_staging(project_root: Path, job_id: str) -> None:
    """Remove only disposable top-level files from one exact job staging directory."""
    if not _SAFE_JOB_ID.fullmatch(job_id):
        raise ArtifactSecurityError("invalid artifact job id")
    canonical_root = project_root.expanduser().resolve(strict=True)
    staging = canonical_root / ".hub" / "staging" / job_id
    if not staging.exists():
        return
    if staging.is_symlink() or not staging.is_dir():
        raise ArtifactSecurityError("job staging directory is untrusted")
    for candidate in staging.iterdir():
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink(missing_ok=True)
    try:
        staging.rmdir()
    except OSError:
        pass
