"""Explicit, force-free external app-server start. No migration or process killing."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

from .codex_control_predicates import unsettled_control
from .delivery_control_predicates import result_ready_control_reconciled
from .release_identity import CURRENT_RELEASE
from .state_connection import connect_existing
from .state_errors import StateError


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
        info = expected.parent.lstat()
        if (
            expected.parent.resolve() != expected.parent
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("unsafe control directory")


def refuse_existing_servers(proc: Path = Path("/proc")) -> None:
    """Conservative same-UID process inventory; unreadable custody refuses start."""
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        try:
            if path.stat().st_uid != os.getuid():
                continue
            argv = (path / "cmdline").read_bytes().split(b"\0")
            if b"app-server" in argv:
                raise ValueError("existing app-server requires separate ownership reconciliation")
        except (FileNotFoundError, ProcessLookupError):
            continue  # Proven process disappearance, not an unreadable live process.
        except OSError:
            raise ValueError("process custody unavailable") from None


def require_idle_state(path: Path) -> None:
    db, _ = connect_existing(path, writable=False, state_error=StateError)
    try:
        db.execute("BEGIN")
        # Archived, explicitly owner-resolved history is closed for admission;
        # it remains uncertain evidence, never native terminality or replay proof.
        busy = db.execute(
            "SELECT 1 FROM provider_jobs j WHERE j.agent_id='codex' AND ("
            "j.status IN ('queued','retry_wait','leased','executing') OR "
            f"(j.status='result_ready' AND NOT {result_ready_control_reconciled('j')}) OR "
            "(j.status='indeterminate' AND NOT EXISTS "
            "(SELECT 1 FROM provider_turn_terminal_evidence e JOIN provider_execution_checkpoints c "
            "ON c.job_id=e.job_id AND c.provider_thread_id=e.provider_thread_id "
            "AND c.provider_turn_id=e.provider_turn_id AND c.project_root=e.project_root WHERE e.job_id=j.job_id) "
            "AND NOT EXISTS (SELECT 1 FROM provider_job_resolutions r JOIN agent_sessions s "
            "ON s.session_id=j.session_id WHERE r.job_id=j.job_id AND s.status='archived' "
            "AND s.agent_id=j.agent_id AND s.generation=j.session_generation "
            "AND r.resolution IN ('acknowledged','superseded','externally_completed')))) LIMIT 1"
        ).fetchone()
        controls = db.execute(
            "SELECT 1 FROM codex_turn_controls control_owner WHERE read_claim_token IS NOT NULL OR "
            f"{unsettled_control('control_owner')} LIMIT 1"
        ).fetchone()
        writers = db.execute(
            "SELECT 1 FROM agent_sessions WHERE agent_id='codex' AND writer_mode!='telegram' "
            "AND status IN ('active','satellite') LIMIT 1"
        ).fetchone()
        if busy or controls or writers:
            raise ValueError("active or unknown Codex work blocks service start")
    finally:
        db.close()


def prepare(manifest: dict, *, clients_closed: bool, now: float | None = None) -> dict:
    if not clients_closed:
        raise ValueError("explicit closed-native-clients assertion required")
    verify_pins(manifest)
    refuse_existing_servers()
    require_idle_state(Path(manifest["state_path"]))
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "run"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--assert-native-clients-closed", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = private_json(args.manifest)
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
        if args.receipt is None:
            raise ValueError("start receipt required")
        check_receipt(manifest, private_json(args.receipt))
        prepare(manifest, clients_closed=True)
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
