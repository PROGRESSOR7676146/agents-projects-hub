"""Explicit, force-free external app-server start. No migration or process killing."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import pwd
import sqlite3
import stat
import sys
import time
from pathlib import Path

from .codex_control_predicates import unsettled_control
from .codex_rollout_completion import completed_rollout_turns
from .delivery_control_predicates import result_ready_control_reconciled
from .release_identity import CURRENT_RELEASE
from .state_connection import connect_existing
from .state_errors import StateError
from .unix_socket_diag import IncompleteEvidence, has_listener_for

BOOT_MARKER = "agents-projects-hub-codex-start"
# A readiness budget, not a bound on the start job: the unit's TimeoutStartSec
# must exceed it, and a loaded boot can need minutes, not one (ADR 0088).
READY_TIMEOUT_SECONDS = 270.0
# How long a process may hide its executable, as during fork-to-exec, before it refuses.
CUSTODY_SETTLE_SECONDS = 2.0


def private_json(path: Path) -> dict:
    info = path.lstat()
    if (
        path.resolve() != path.absolute()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o777 != 0o600
        or info.st_nlink != 1
        or info.st_size > 16384
    ):
        raise ValueError("unsafe service input")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("invalid service input")
    return value


def digest(value: dict) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _artifact_sha256(path: Path) -> str:
    """Stream the hash; the pinned Codex binary is hundreds of megabytes."""
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_manifest(manifest: dict) -> Path:
    """Static pins and home/socket relation; returns the expected socket path."""
    if manifest.get("version") != 1:
        raise ValueError("unsupported service manifest")
    for name in ("codex_binary", "tlive_daemon"):
        pin = manifest[name]
        path = Path(pin["path"])
        info = path.lstat()
        if (
            not path.is_absolute()
            or path.resolve() != path
            or not stat.S_ISREG(info.st_mode)
            or info.st_mode & 0o222
            or _artifact_sha256(path) != pin["sha256"]
        ):
            raise ValueError(f"service artifact pin mismatch: {name}")
    home = Path(manifest["codex_home"])
    if not home.is_absolute() or home.resolve() != home or not home.is_dir():
        raise ValueError("invalid Codex home")
    info = home.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe Codex home")
    expected = home / "app-server-control" / "app-server-control.sock"
    if manifest["socket_path"] != str(expected):
        raise ValueError("Codex socket and home mismatch")
    return expected


def verify_pins(manifest: dict, verified: Path | None = None) -> None:
    """``verified`` is the result of a verify_manifest already run in this start."""
    expected = verify_manifest(manifest) if verified is None else verified
    if os.path.lexists(expected):
        raise ValueError("existing socket or path requires separate ownership reconciliation")
    if expected.parent.exists():
        _private_directory(expected.parent)


def _user_manager(argv: list[bytes]) -> bool:
    return argv[:1] == [b"(sd-pam)"] or (
        bool(argv) and os.path.basename(argv[0]) == b"systemd" and b"--user" in argv
    )


def _read_process(path: Path) -> tuple[list[bytes], bytes] | None:
    """(argv, executable) of a same-UID process, or None when it is not one.

    A process caught between the service manager's fork and its exec can hide
    its executable for a moment; only one that stays unreadable for
    CUSTODY_SETTLE_SECONDS refuses. A vanished process is proven absence.
    """
    deadline = time.monotonic() + CUSTODY_SETTLE_SECONDS
    while True:
        argv: list[bytes] = []
        try:
            if path.stat().st_uid != os.getuid():
                return None
            argv = (path / "cmdline").read_bytes().split(b"\0")
            try:
                return argv, os.fsencode(os.readlink(path / "exe"))
            except FileNotFoundError:
                return argv, b""  # Kernel thread or exited after cmdline.
        except (FileNotFoundError, ProcessLookupError):
            return None
        except PermissionError:
            # Only the non-dumpable per-user manager may hide its executable for good.
            if _user_manager(argv):
                return argv, b""
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _same_uid_processes(proc: Path) -> list[tuple[str, list[bytes], bytes]]:
    """(pid, argv, executable) of other same-UID processes; unreadable custody refuses."""
    found = []
    for path in proc.iterdir():
        if not path.name.isdigit() or int(path.name) == os.getpid():
            continue
        try:
            process = _read_process(path)
        except OSError as exc:
            reason = errno.errorcode.get(exc.errno or 0, type(exc).__name__)
            raise ValueError(f"process custody unavailable: pid {path.name} ({reason})") from None
        if process is not None:
            found.append((path.name, *process))
    return found


def refuse_existing_servers(proc: Path = Path("/proc")) -> None:
    """Conservative same-UID process inventory; unreadable custody refuses start."""
    # Refusals name only the PID: process titles are writable, unredacted data.
    for pid, argv, _ in _same_uid_processes(proc):
        if b"app-server" in argv:
            raise ValueError(
                f"existing app-server requires separate ownership reconciliation: pid {pid}"
            )


def refuse_native_clients(manifest: dict, proc: Path = Path("/proc")) -> None:
    """Prove closed clients without an operator; fail closed on any Codex or tlive.

    Any installation counts, not only the pinned one: a process whose
    executable, program or script name starts with ``codex`` or contains
    ``tlive`` refuses, as does any argument naming a pinned artifact.
    """
    pinned = {os.fsencode(manifest[name]["path"]) for name in ("codex_binary", "tlive_daemon")}
    for pid, argv, executable in _same_uid_processes(proc):
        names = [os.path.basename(item).lower() for item in (executable, *argv[:2]) if item]
        if pinned.intersection(argv) or any(
            name.startswith(b"codex") or b"tlive" in name for name in names
        ):
            raise ValueError(f"a native Codex or tlive client is running: pid {pid}")


def boot_identity(proc: Path = Path("/proc")) -> tuple[str, float]:
    """Return (boot ID, boot time in epoch seconds) from the kernel."""
    boot = (proc / "sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    for line in (proc / "stat").read_text(encoding="ascii").splitlines():
        if line.startswith("btime "):
            return boot, float(line.split()[1])
    raise ValueError("boot time unavailable")


def require_lingering(linger_dir: Path = Path("/var/lib/systemd/linger")) -> None:
    """Without lingering the runtime directory dies at logout, not at reboot."""
    if not (linger_dir / pwd.getpwuid(os.getuid()).pw_name).exists():
        raise ValueError("boot start requires user lingering")


def claim_first_start(runtime_dir: Path, boot: str) -> bool:
    """Record a start attempt in this boot; True only for the first attempt.

    The marker lives in the per-user runtime directory, which the system
    clears on reboot. It is consumed by the attempt, not by success, so a
    refused or crashed first start needs the explicit plan + run.
    """
    info = runtime_dir.lstat()
    if (
        not runtime_dir.is_absolute()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("unsafe runtime directory")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        fd = os.open(runtime_dir / BOOT_MARKER, flags, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="ascii") as marker:
        marker.write(boot + "\n")
    return True


def _private_directory(path: Path) -> tuple[int, int]:
    info = path.lstat()
    if (
        path.resolve() != path
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("unsafe control directory")
    return info.st_dev, info.st_ino


def remove_previous_boot_link(socket_path: Path, booted_at: float) -> bool:
    """Remove only an owned dangling link that predates this boot.

    The real socket lives on a filesystem the reboot cleared; its link in the
    Codex home survives and would block every start. A socket, a regular file,
    a link with a live target or a link from this boot is left alone, and the
    normal pins then refuse it. Returns whether the link was removed.
    """
    try:
        info = socket_path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISLNK(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_ctime >= booted_at
        or os.path.exists(socket_path)
    ):
        return False
    parent = _private_directory(socket_path.parent)
    fd = os.open(socket_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = os.stat(socket_path.name, dir_fd=fd, follow_symlinks=False)
        if (
            (os.fstat(fd).st_dev, os.fstat(fd).st_ino) != parent
            or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
            or not stat.S_ISLNK(current.st_mode)
        ):
            raise ValueError("control path changed during cleanup")
        os.unlink(socket_path.name, dir_fd=fd)
    finally:
        os.close(fd)
    return True


def wait_ready(
    manifest: dict,
    *,
    timeout_seconds: float = READY_TIMEOUT_SECONDS,
    main_pid: int | None = None,
    proc: Path = Path("/proc"),
) -> None:
    """Return once a kernel listener serves the manifest socket; never connect.

    The wait covers the start gates as well as Codex itself, because systemd
    runs this beside the main process. A main process that already exited
    (a refused start) ends the wait at once.
    """
    path = Path(manifest["socket_path"])
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if main_pid is not None and not (proc / str(main_pid)).exists():
            raise ValueError("Codex app-server exited before it listened")
        try:
            info = path.stat()
            if stat.S_ISSOCK(info.st_mode) and has_listener_for(info):
                return
        except (FileNotFoundError, IncompleteEvidence):
            pass
        time.sleep(0.2)
    raise ValueError("Codex app-server did not become ready")


def require_idle_state(path: Path, *, codex_home: Path | None = None) -> None:
    db, _ = connect_existing(path, writable=False, state_error=StateError)
    db.row_factory = sqlite3.Row
    try:
        db.execute("BEGIN")
        # Archived, explicitly owner-resolved history is closed for admission;
        # it remains uncertain evidence, never native terminality or replay proof.
        busy = db.execute(
            "SELECT j.job_id,j.status FROM provider_jobs j WHERE j.agent_id='codex' AND ("
            "j.status IN ('queued','retry_wait','leased','executing') OR "
            f"(j.status='result_ready' AND NOT {result_ready_control_reconciled('j')}) OR "
            "(j.status='indeterminate' AND NOT EXISTS "
            "(SELECT 1 FROM provider_turn_terminal_evidence e JOIN provider_execution_checkpoints c "
            "ON c.job_id=e.job_id AND c.provider_thread_id=e.provider_thread_id "
            "AND c.provider_turn_id=e.provider_turn_id AND c.project_root=e.project_root WHERE e.job_id=j.job_id) "
            "AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions r JOIN agent_sessions s "
            "ON s.session_id=j.session_id WHERE r.job_id=j.job_id AND s.status='archived' "
            "AND s.agent_id=j.agent_id AND s.generation=j.session_generation "
            "AND r.resolution IN ('acknowledged','superseded','externally_completed'))))"
        ).fetchall()
        controls = db.execute(
            "SELECT 1 FROM codex_turn_controls control_owner WHERE read_claim_token IS NOT NULL OR "
            f"{unsettled_control('control_owner')} LIMIT 1"
        ).fetchone()
        writers = db.execute(
            "SELECT 1 FROM agent_sessions WHERE agent_id='codex' AND writer_mode!='telegram' "
            "AND status IN ('active','satellite') LIMIT 1"
        ).fetchone()
        if controls:
            raise ValueError("active or unknown Codex work blocks service start: turn control")
        if writers:
            raise ValueError("active or unknown Codex work blocks service start: native writer")
        completions: dict[tuple[str, str], frozenset[str]] = {}
        for job in busy:
            if codex_home is None or job["status"] != "indeterminate":
                raise ValueError(
                    f"active or unknown Codex work blocks service start: {job['status']} job"
                )
            checkpoint = db.execute(
                "SELECT c.provider_thread_id,c.provider_turn_id,c.project_root "
                "FROM provider_jobs j JOIN provider_execution_checkpoints c USING(job_id) "
                "JOIN provider_job_resolutions r USING(job_id) "
                "JOIN agent_sessions s ON s.session_id=j.session_id "
                "WHERE j.job_id=? AND r.resolution='externally_completed' "
                "AND s.agent_id=j.agent_id AND s.generation=j.session_generation "
                "AND s.provider_session_id=c.provider_thread_id",
                (job["job_id"],),
            ).fetchone()
            if checkpoint is None or not checkpoint["provider_turn_id"]:
                raise ValueError(
                    "active or unknown Codex work blocks service start: indeterminate job"
                )
            key = checkpoint["provider_thread_id"], checkpoint["project_root"]
            if key not in completions:
                completions[key] = completed_rollout_turns(codex_home, *key)
            if checkpoint["provider_turn_id"] not in completions[key]:
                raise ValueError(
                    "active or unknown Codex work blocks service start: indeterminate job"
                )
    finally:
        db.close()


def prepare(
    manifest: dict, *, clients_closed: bool, now: float | None = None, verified: Path | None = None
) -> dict:
    if not clients_closed:
        raise ValueError("explicit closed-native-clients assertion required")
    verify_pins(manifest, verified)
    refuse_existing_servers()
    require_idle_state(Path(manifest["state_path"]), codex_home=Path(manifest["codex_home"]))
    timestamp = time.time() if now is None else now
    return dict(
        version=1,
        manifest_sha256=digest(manifest),
        created_at=timestamp,
        expires_at=timestamp + 60,
        native_clients_closed=True,
    )


def check_receipt(manifest: dict, receipt: dict, *, now: float | None = None) -> None:
    timestamp = time.time() if now is None else now
    created, expires = receipt.get("created_at"), receipt.get("expires_at")
    if (
        isinstance(created, bool)
        or not isinstance(created, (int, float))
        or isinstance(expires, bool)
        or not isinstance(expires, (int, float))
    ):
        raise ValueError("stale or mismatched start receipt")
    if (
        receipt.get("version") != 1
        or receipt.get("manifest_sha256") != digest(manifest)
        or receipt.get("native_clients_closed") is not True
        or not created <= timestamp < expires <= created + 60
    ):
        raise ValueError("stale or mismatched start receipt")


def _receipt_valid(manifest: dict, receipt: Path | None) -> bool:
    if receipt is None:
        return False
    try:
        check_receipt(manifest, private_json(receipt))
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


def authorize_start(manifest: dict, receipt: Path | None, runtime_dir: Path) -> None:
    """Authorize by a fresh operator receipt, or as the first start of this boot.

    Without a valid receipt, only the first attempt of a boot may start, and
    only after proving closed clients itself. Both modes then pass the same
    pins, server refusal and exact idle state.
    """
    boot, booted_at = boot_identity()
    first = claim_first_start(runtime_dir, boot)
    verified = None
    if not _receipt_valid(manifest, receipt):
        if not first:
            raise ValueError("a fresh start receipt is required after the first start of a boot")
        require_lingering()
        refuse_native_clients(manifest)
        refuse_existing_servers()
        # Hash the pins once per start: the same verification gates the cleanup and prepare.
        verified = verify_manifest(manifest)
        remove_previous_boot_link(verified, booted_at)
    prepare(manifest, clients_closed=True, verified=verified)


def refusal_reason(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        detail = os.strerror(exc.errno) if exc.errno else type(exc).__name__
        return f"{detail}: {exc.filename}" if exc.filename else detail
    if isinstance(exc, KeyError):
        return f"missing manifest field {exc.args[0] if exc.args else ''}"
    return str(exc) or type(exc).__name__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "run", "ready"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--assert-native-clients-closed", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = private_json(args.manifest)
        if args.action == "ready":
            pid = os.environ.get("MAINPID", "")
            wait_ready(manifest, main_pid=int(pid) if pid.isdigit() else None)
            return 0
        if not CURRENT_RELEASE.clean_tree or CURRENT_RELEASE.git_sha != manifest["hub_git_sha"]:
            raise ValueError("immutable Hub release pin mismatch")
        if args.action == "plan":
            print(
                json.dumps(
                    prepare(manifest, clients_closed=args.assert_native_clients_closed),
                    sort_keys=True,
                )
            )
            return 0
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime:
            raise ValueError("per-user runtime directory unavailable")
        authorize_start(manifest, args.receipt, Path(runtime))
        os.environ["CODEX_HOME"] = manifest["codex_home"]
        binary = manifest["codex_binary"]["path"]
        os.execv(binary, [binary, "app-server", "--listen", "unix://"])
    except (OSError, ValueError, KeyError, TypeError, StateError) as exc:
        # The journal is the only record of a refused boot start: name the gate.
        outcome = "readiness failed" if args.action == "ready" else "start refused"
        print(f"external Codex {outcome}: {refusal_reason(exc)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
