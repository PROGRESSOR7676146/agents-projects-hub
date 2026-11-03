"""Explicit, force-free external app-server start. No migration or process killing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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


def verify_pins(manifest: dict) -> None:
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
            or hashlib.sha256(path.read_bytes()).hexdigest() != pin["sha256"]
        ):
            raise ValueError("service artifact pin mismatch")
    home = Path(manifest["codex_home"])
    if not home.is_absolute() or home.resolve() != home or not home.is_dir():
        raise ValueError("invalid Codex home")
    info = home.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("unsafe Codex home")
    expected = home / "app-server-control" / "app-server-control.sock"
    if manifest["socket_path"] != str(expected):
        raise ValueError("Codex socket and home mismatch")
    if os.path.lexists(expected):
        raise ValueError("existing socket or path requires separate ownership reconciliation")
    if expected.parent.exists():
        _private_directory(expected.parent)


def _same_uid_argv(proc: Path) -> list[list[bytes]]:
    """Argument vectors of all same-UID processes; unreadable custody refuses."""
    found = []
    for path in proc.iterdir():
        if not path.name.isdigit() or int(path.name) == os.getpid():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            found.append((path / "cmdline").read_bytes().split(b"\0"))
        except (FileNotFoundError, ProcessLookupError):
            continue  # Proven process disappearance, not an unreadable live process.
        except OSError:
            raise ValueError("process custody unavailable") from None
    return found


def refuse_existing_servers(proc: Path = Path("/proc")) -> None:
    """Conservative same-UID process inventory; unreadable custody refuses start."""
    for argv in _same_uid_argv(proc):
        if b"app-server" in argv:
            raise ValueError("existing app-server requires separate ownership reconciliation")


def refuse_native_clients(manifest: dict, proc: Path = Path("/proc")) -> None:
    """Prove closed clients without an operator: no pinned Codex or tlive runs."""
    pinned = {os.fsencode(manifest[name]["path"]) for name in ("codex_binary", "tlive_daemon")}
    for argv in _same_uid_argv(proc):
        if pinned.intersection(argv):
            raise ValueError("a native Codex or tlive client is running")


def boot_identity(proc: Path = Path("/proc")) -> tuple[str, float]:
    """Return (boot ID, boot time in epoch seconds) from the kernel."""
    boot = (proc / "sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    for line in (proc / "stat").read_text(encoding="ascii").splitlines():
        if line.startswith("btime "):
            return boot, float(line.split()[1])
    raise ValueError("boot time unavailable")


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


def wait_ready(manifest: dict, *, timeout_seconds: float = 60.0) -> None:
    """Return once a kernel listener serves the manifest socket; never connect."""
    path = Path(manifest["socket_path"])
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
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
        if controls or writers:
            raise ValueError("active or unknown Codex work blocks service start")
        completions: dict[tuple[str, str], frozenset[str]] = {}
        for job in busy:
            if codex_home is None or job["status"] != "indeterminate":
                raise ValueError("active or unknown Codex work blocks service start")
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
                raise ValueError("active or unknown Codex work blocks service start")
            key = checkpoint["provider_thread_id"], checkpoint["project_root"]
            if key not in completions:
                completions[key] = completed_rollout_turns(codex_home, *key)
            if checkpoint["provider_turn_id"] not in completions[key]:
                raise ValueError("active or unknown Codex work blocks service start")
    finally:
        db.close()


def prepare(manifest: dict, *, clients_closed: bool, now: float | None = None) -> dict:
    if not clients_closed:
        raise ValueError("explicit closed-native-clients assertion required")
    verify_pins(manifest)
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
    if not _receipt_valid(manifest, receipt):
        if not first:
            raise ValueError("a fresh start receipt is required after the first start of a boot")
        refuse_native_clients(manifest)
        refuse_existing_servers()
        remove_previous_boot_link(Path(manifest["socket_path"]), booted_at)
    prepare(manifest, clients_closed=True)


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
            wait_ready(manifest)
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
    except (OSError, ValueError, KeyError, TypeError, StateError):
        print(
            "external Codex start refused; inspect pins, custody and exact idle evidence",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
