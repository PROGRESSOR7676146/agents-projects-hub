from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Sequence

from . import privacy_scan

ROOT = Path(__file__).resolve().parents[2]
POLICY_FILE_ENV = "HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE"
POLICY_GIT_CONFIG = "hub.publicAuthorEmailFile"
REPOSITORY_VARIABLE = "HUB_PUBLIC_GIT_AUTHOR_EMAIL"
# Versioned hooks installed together as one set.
MANAGED_HOOKS = ("pre-commit", "pre-push")
# Installed sets live under the Git common directory. Each set is a complete,
# immutable directory named by its content digest; Git runs hooks through the
# ``active`` link, which one atomic rename switches from one set to the next.
HOOK_ROOT = "hub-hooks"
# The single mutable hook directory used before sets; removed on migration.
LEGACY_HOOK_DIRECTORY = "hub-managed-hooks"
_ZERO_OID = "0" * 40
_GITHUB_REMOTE = re.compile(
    r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)$"
)


class PreflightError(RuntimeError):
    pass


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PreflightError("required local or hosted publication check is unavailable") from error


def _run_bytes(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: int = 60,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PreflightError("required hosted publication check is unavailable") from error


def _policy_path(root: Path) -> Path:
    configured = os.environ.get(POLICY_FILE_ENV)
    if configured:
        return Path(configured)
    result = _run(
        ["git", "config", "--local", "--get", POLICY_GIT_CONFIG],
        cwd=root,
    )
    if result.returncode != 0:
        raise PreflightError("public author policy is not configured for this checkout")
    value = result.stdout.removesuffix("\n")
    if not value or "\n" in value or "\r" in value:
        raise PreflightError("public author policy configuration is invalid")
    return Path(value)


def _read_declared_email(root: Path, policy_path: Path) -> bytes:
    previous = os.environ.get(POLICY_FILE_ENV)
    os.environ[POLICY_FILE_ENV] = str(policy_path)
    try:
        value = privacy_scan._read_public_author_email(root)
    finally:
        if previous is None:
            os.environ.pop(POLICY_FILE_ENV, None)
        else:
            os.environ[POLICY_FILE_ENV] = previous
    if value is None:
        raise PreflightError("public author policy file failed validation")
    return value


def _github_repository(remote_url: str | None) -> str | None:
    if not remote_url:
        return None
    if remote_url.endswith(".git"):
        remote_url = remote_url[:-4]
    match = _GITHUB_REMOTE.fullmatch(remote_url)
    if match is None:
        raise PreflightError("push remote is not a supported GitHub repository URL")
    return match.group(1)


def _repository_from_gh(root: Path) -> str:
    result = _run_bytes(["gh", "repo", "view", "--json", "nameWithOwner"], cwd=root)
    if result.returncode != 0:
        raise PreflightError("GitHub repository identity is unavailable")
    try:
        payload = json.loads(result.stdout)
        repository = payload["nameWithOwner"]
    except (KeyError, TypeError, ValueError) as error:
        raise PreflightError("GitHub repository identity response is invalid") from error
    if (
        not isinstance(repository, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
    ):
        raise PreflightError("GitHub repository identity response is invalid")
    return repository


def _require_matching_repository_variable(
    declared_email: bytes,
    root: Path,
    repository: str | None = None,
) -> None:
    selected_repository = repository or _repository_from_gh(root)
    endpoint = f"repos/{selected_repository}/actions/variables/{REPOSITORY_VARIABLE}"
    result = _run_bytes(["gh", "api", "--method", "GET", endpoint], cwd=root)
    if result.returncode != 0:
        raise PreflightError("required repository variable is absent or unavailable")
    try:
        payload = json.loads(result.stdout)
        raw_value = payload["value"]
    except (KeyError, TypeError, ValueError) as error:
        raise PreflightError("repository variable response is invalid") from error
    if not isinstance(raw_value, str):
        raise PreflightError("repository variable response is invalid")
    remote_email = privacy_scan._parse_public_author_email(raw_value.encode("utf-8"))
    if remote_email is None or remote_email != declared_email:
        raise PreflightError("repository variable does not match the local publication policy")


def _validate_push_refs(
    source: str,
    head: str,
    resolve_commit: Callable[[str], str] | None = None,
    resolve_ref: Callable[[str], str] | None = None,
) -> None:
    resolver = resolve_commit or (lambda oid: oid)
    for line in source.splitlines():
        fields = line.split()
        if len(fields) != 4:
            raise PreflightError("pre-push reference input is malformed")
        _local_ref, local_oid, _remote_ref, _remote_oid = fields
        if local_oid == _ZERO_OID:
            continue
        if not _local_ref.startswith("refs/"):
            raise PreflightError("published objects must be reachable through a local ref")
        if resolve_ref is not None and resolve_ref(_local_ref) != local_oid:
            raise PreflightError("published objects must match their checked local ref")
        if resolver(local_oid) != head:
            raise PreflightError("every published reference must resolve to the checked-out HEAD")


def _require_checkout_import(root: Path) -> None:
    expected = (root / "src" / "hermes_codex_router").resolve(strict=True)
    imported = Path(privacy_scan.__file__).resolve(strict=True)
    if expected not in imported.parents:
        raise PreflightError("Python imported validation code from another checkout")


def _git_stdout(root: Path, *args: str) -> str:
    result = _run(["git", *args], cwd=root)
    if result.returncode != 0:
        raise PreflightError("Git publication state check failed")
    return result.stdout.removesuffix("\n")


def _resolve_exact_ref(root: Path, ref: str) -> str:
    shape = _run(["git", "check-ref-format", ref], cwd=root)
    if shape.returncode != 0:
        raise PreflightError("published objects must match their checked local ref")
    result = _run(["git", "show-ref", "--verify", "--hash", ref], cwd=root)
    oid = result.stdout.removesuffix("\n")
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40,64}", oid) is None:
        raise PreflightError("published objects must match their checked local ref")
    return oid


def _resolve_commit_without_replacements(root: Path, oid: str) -> str:
    return _git_stdout(root, "--no-replace-objects", "rev-parse", f"{oid}^{{commit}}")


def _registered_worktrees(root: Path) -> list[Path]:
    result = _run_bytes(["git", "worktree", "list", "--porcelain", "-z"], cwd=root)
    if result.returncode != 0:
        raise PreflightError("could not enumerate registered Git worktrees")
    paths: list[Path] = []
    for record in result.stdout.split(b"\0\0"):
        fields = [field for field in record.split(b"\0") if field]
        if not fields or any(field.startswith(b"prunable") for field in fields):
            continue
        worktree_fields = [field for field in fields if field.startswith(b"worktree ")]
        if len(worktree_fields) != 1:
            raise PreflightError("Git worktree inventory is malformed")
        paths.append(Path(os.fsdecode(worktree_fields[0].removeprefix(b"worktree "))))
    if not paths:
        raise PreflightError("Git reported no active registered worktrees")
    return paths


def _worktree_config_enabled(root: Path) -> bool:
    extension = _run(
        ["git", "config", "--bool", "--get", "extensions.worktreeConfig"],
        cwd=root,
    )
    if extension.returncode not in (0, 1):
        raise PreflightError("could not inspect Git worktree configuration")
    return extension.returncode == 0 and extension.stdout.strip() == "true"


@dataclass(frozen=True)
class _Setting:
    """One Git configuration key in one scope of one worktree."""

    cwd: Path
    scope: str
    key: str


def _read_setting(setting: _Setting) -> str | None:
    result = _run(["git", "config", setting.scope, "--get", setting.key], cwd=setting.cwd)
    if result.returncode == 0:
        return result.stdout.removesuffix("\n")
    if result.returncode == 1:
        return None
    raise PreflightError("could not read the repository Git configuration")


def _write_setting(setting: _Setting, value: str | None) -> None:
    if value is None:
        argv = ["git", "config", setting.scope, "--unset", setting.key]
        accepted = (0, 5)
    else:
        argv = ["git", "config", setting.scope, setting.key, value]
        accepted = (0,)
    if _run(argv, cwd=setting.cwd).returncode not in accepted:
        raise PreflightError("could not install the repository publication hook")


def _read_hook_sources(root: Path) -> dict[str, bytes]:
    # Read every versioned hook before installing any, so a failure never
    # leaves one gate updated and the other stale.
    sources: dict[str, bytes] = {}
    for name in MANAGED_HOOKS:
        source = root / ".githooks" / name
        try:
            source_details = source.lstat()
            if not stat.S_ISREG(source_details.st_mode):
                raise PreflightError(f"versioned {name} hook is unavailable")
            sources[name] = source.read_bytes()
        except OSError as error:
            raise PreflightError(f"versioned {name} hook is unavailable") from error
    return sources


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, exist_ok=True)
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid():
        raise PreflightError("shared Git hook directory failed validation")
    os.chmod(path, 0o700)
    return path


@contextmanager
def _install_lock(directory: Path) -> Iterator[None]:
    try:
        descriptor = os.open(directory / ".install.lock", os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as error:
        raise PreflightError("could not install the shared publication hook") from error
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise PreflightError("another publication hook installation is running") from None
        yield
    finally:
        os.close(descriptor)


def _require_hook_set(directory: Path, sources: dict[str, bytes]) -> None:
    try:
        details = directory.lstat()
        valid = (
            stat.S_ISDIR(details.st_mode)
            and details.st_uid == os.getuid()
            and sorted(path.name for path in directory.iterdir()) == sorted(sources)
        )
        for name, content in sources.items():
            if not valid:
                break
            file_details = (directory / name).lstat()
            valid = (
                stat.S_ISREG(file_details.st_mode)
                and stat.S_IMODE(file_details.st_mode) == 0o700
                and (directory / name).read_bytes() == content
            )
    except OSError:
        valid = False
    if not valid:
        raise PreflightError("an installed publication hook set failed validation")


def _materialize_hook_set(sets: Path, sources: dict[str, bytes]) -> tuple[Path, bool]:
    """Return a complete immutable directory holding exactly ``sources``.

    The set is written and synced under a temporary name and appears under its
    content digest in one rename, so no reader ever sees a partial set.
    """
    digest = hashlib.sha256()
    for name in sorted(sources):
        digest.update(name.encode() + b"\0" + hashlib.sha256(sources[name]).digest())
    target = sets / digest.hexdigest()[:32]
    if os.path.lexists(target):
        _require_hook_set(target, sources)
        return target, False
    staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=sets))
    try:
        for name, content in sources.items():
            descriptor = os.open(staging / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o700)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        _sync_directory(staging)
        os.rename(staging, target)
        _sync_directory(sets)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return target, True


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _link_target(link: Path) -> str | None:
    try:
        details = link.lstat()
        if not stat.S_ISLNK(details.st_mode):
            raise PreflightError("the active publication hook link failed validation")
        return os.readlink(link)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise PreflightError("could not read the active publication hook link") from error


def _point_link(link: Path, target: str) -> None:
    """Atomically make ``link`` a symbolic link to ``target``."""
    temporary = link.with_name(f".{link.name}-{secrets.token_hex(8)}")
    os.symlink(target, temporary)
    try:
        os.replace(temporary, link)
    except BaseException:
        _remove_quietly(temporary)
        raise
    _sync_directory(link.parent)


def _verify_installation(
    worktrees: Sequence[Path], active: Path, target: str, sources: dict[str, bytes]
) -> None:
    if _link_target(active) != target:
        raise PreflightError("the active publication hook link failed verification")
    _require_hook_set(active.parent / target, sources)
    for worktree in worktrees:
        result = _run(["git", "config", "--get", "core.hooksPath"], cwd=worktree)
        if result.returncode != 0 or result.stdout.removesuffix("\n") != str(active):
            raise PreflightError("a registered worktree overrides the publication hook")


def _roll_back(
    active: Path, previous_link: str | None, previous: dict[_Setting, str | None]
) -> None:
    """Return to the exact previous hook link and settings, or say it could not.

    Settings are restored first and read back. A link that this installation
    created is removed only after that, when no setting can still point at
    it; if the settings cannot be restored, the link keeps pointing at the
    complete new set, so every worktree still runs a complete gate. A link
    that existed before is switched back to its previous complete set. Hook
    sets are never deleted here.
    """
    for setting, value in previous.items():
        with suppress(PreflightError):
            if _read_setting(setting) != value:
                _write_setting(setting, value)
    try:
        settings_restored = all(
            _read_setting(setting) == value for setting, value in previous.items()
        )
    except PreflightError:
        settings_restored = False
    if settings_restored or previous_link is not None:
        with suppress(OSError, PreflightError):
            if _link_target(active) != previous_link:
                if previous_link is None:
                    active.unlink()
                else:
                    _point_link(active, previous_link)
    try:
        link_restored = _link_target(active) == previous_link
    except PreflightError:
        link_restored = False
    if not (settings_restored and link_restored):
        raise PreflightError(
            "hook installation failed and the previous hook configuration could not be "
            "confirmed restored; check core.hooksPath in every worktree before committing"
        )


def _prune(sets: Path, keep: Path, legacy: Path) -> None:
    """Remove hook sets nothing points to any more; failures leave harmless files."""
    for entry in sets.iterdir():
        if entry != keep and entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
    for stale in sets.parent.glob(".active-*"):
        _remove_quietly(stale)
    if legacy.is_dir() and not legacy.is_symlink():
        shutil.rmtree(legacy, ignore_errors=True)


def _install(root: Path) -> None:
    policy = _policy_path(root)
    _read_declared_email(root, policy)
    interpreter = Path(sys.executable)
    if (
        not interpreter.is_absolute()
        or not interpreter.is_file()
        or not os.access(interpreter, os.X_OK)
    ):
        raise PreflightError("publication Python interpreter failed validation")
    sources = _read_hook_sources(root)
    common = Path(_git_stdout(root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = root / common
    try:
        common = common.resolve(strict=True)
        hook_root = _private_directory(common / HOOK_ROOT)
        sets = _private_directory(hook_root / "sets")
    except OSError as error:
        raise PreflightError("could not install the shared publication hook") from error
    active = hook_root / "active"
    values = {
        "core.hooksPath": str(active),
        POLICY_GIT_CONFIG: str(policy),
        "hub.publishPython": str(interpreter),
    }
    targets = {_Setting(root, "--local", key): value for key, value in values.items()}
    worktrees = _registered_worktrees(root)
    if _worktree_config_enabled(root):
        # A per-worktree value overrides the shared one, so each is set too.
        for worktree in worktrees:
            targets[_Setting(worktree, "--worktree", "core.hooksPath")] = str(active)
    with _install_lock(hook_root):
        previous = {setting: _read_setting(setting) for setting in targets}
        previous_link = _link_target(active)
        created: Path | None = None
        # The complete new set becomes active in one rename of the link. The
        # previous set and every earlier setting stay intact until the whole
        # installation is verified, so a failure at any step can return to them.
        try:
            try:
                hook_set, is_new = _materialize_hook_set(sets, sources)
                created = hook_set if is_new else None
                target = f"{sets.name}/{hook_set.name}"
                _point_link(active, target)
            except OSError as error:
                raise PreflightError("could not install the shared publication hook") from error
            for setting, value in targets.items():
                _write_setting(setting, value)
            _verify_installation(worktrees, active, target, sources)
        except BaseException:
            _roll_back(active, previous_link, previous)
            if created is not None:
                shutil.rmtree(created, ignore_errors=True)
            raise
        _prune(sets, hook_set, common / LEGACY_HOOK_DIRECTORY)


def _remove_quietly(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed publication preflight")
    parser.add_argument("remote_name", nargs="?")
    parser.add_argument("remote_url", nargs="?")
    parser.add_argument("--install", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _require_checkout_import(ROOT)
        if args.install:
            _install(ROOT)
            print("Publication preflight hook installed.")
            return 0

        status = _run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=ROOT,
        )
        if status.returncode != 0 or status.stdout:
            raise PreflightError("the publication checkout must be clean")
        head = _git_stdout(ROOT, "rev-parse", "HEAD")
        source = sys.stdin.read()

        def resolve_commit(oid: str) -> str:
            return _resolve_commit_without_replacements(ROOT, oid)

        def resolve_ref(ref: str) -> str:
            return _resolve_exact_ref(ROOT, ref)

        _validate_push_refs(source, head, resolve_commit, resolve_ref)
        policy_path = _policy_path(ROOT)
        declared_email = _read_declared_email(ROOT, policy_path)
        repository = _github_repository(args.remote_url)
        _require_matching_repository_variable(declared_email, ROOT, repository)

        environment = os.environ.copy()
        existing_pythonpath = environment.get("PYTHONPATH")
        source_root = str(ROOT / "src")
        environment["PYTHONPATH"] = (
            source_root
            if not existing_pythonpath
            else source_root + os.pathsep + existing_pythonpath
        )
        environment[POLICY_FILE_ENV] = str(policy_path)
        validator = _run(
            [sys.executable, str(ROOT / "scripts" / "validate.py")],
            cwd=ROOT,
            env=environment,
            timeout=900,
        )
        sys.stdout.write(validator.stdout)
        sys.stderr.write(validator.stderr)
        if validator.returncode != 0:
            raise PreflightError("canonical repository validation failed")
        final_status = _run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=ROOT,
        )
        if final_status.returncode != 0 or final_status.stdout:
            raise PreflightError("the publication checkout changed during validation")
        if _git_stdout(ROOT, "rev-parse", "HEAD") != head:
            raise PreflightError("the checked-out HEAD changed during validation")
        _validate_push_refs(source, head, resolve_commit, resolve_ref)
        if _policy_path(ROOT) != policy_path:
            raise PreflightError("the publication policy configuration changed during validation")
        if _read_declared_email(ROOT, policy_path) != declared_email:
            raise PreflightError("the publication policy file changed during validation")
    except PreflightError as error:
        print(f"Publication preflight failed: {error}", file=sys.stderr)
        return 1
    print(f"Publication preflight passed for {head}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
