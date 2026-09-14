from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, Sequence

from . import privacy_scan

ROOT = Path(__file__).resolve().parents[2]
POLICY_FILE_ENV = "HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE"
POLICY_GIT_CONFIG = "hub.publicAuthorEmailFile"
REPOSITORY_VARIABLE = "HUB_PUBLIC_GIT_AUTHOR_EMAIL"
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


def _configure_all_worktree_hooks(root: Path, hook_directory: Path) -> None:
    extension = _run(
        ["git", "config", "--bool", "--get", "extensions.worktreeConfig"],
        cwd=root,
    )
    if extension.returncode not in (0, 1):
        raise PreflightError("could not inspect Git worktree configuration")
    worktrees = _registered_worktrees(root)
    if extension.returncode == 0 and extension.stdout.strip() == "true":
        for worktree in worktrees:
            result = _run(
                ["git", "config", "--worktree", "core.hooksPath", str(hook_directory)],
                cwd=worktree,
            )
            if result.returncode != 0:
                raise PreflightError("could not update a registered worktree hook")
    for worktree in worktrees:
        result = _run(["git", "config", "--get", "core.hooksPath"], cwd=worktree)
        if result.returncode != 0 or result.stdout.removesuffix("\n") != str(hook_directory):
            raise PreflightError("a registered worktree overrides the publication hook")


def _install(root: Path) -> None:
    policy = _policy_path(root)
    _read_declared_email(root, policy)
    source = root / ".githooks" / "pre-push"
    interpreter = Path(sys.executable)
    if (
        not interpreter.is_absolute()
        or not interpreter.is_file()
        or not os.access(interpreter, os.X_OK)
    ):
        raise PreflightError("publication Python interpreter failed validation")
    try:
        source_details = source.lstat()
    except OSError as error:
        raise PreflightError("versioned publication hook is unavailable") from error
    if not stat.S_ISREG(source_details.st_mode):
        raise PreflightError("versioned publication hook is unavailable")
    common_raw = _git_stdout(root, "rev-parse", "--git-common-dir")
    common = Path(common_raw)
    if not common.is_absolute():
        common = root / common
    try:
        common = common.resolve(strict=True)
        hook_directory = common / "hub-managed-hooks"
        hook_directory.mkdir(mode=0o700, exist_ok=True)
        directory_details = hook_directory.lstat()
        if not stat.S_ISDIR(directory_details.st_mode) or directory_details.st_uid != os.getuid():
            raise PreflightError("shared Git hook directory failed validation")
        os.chmod(hook_directory, 0o700)
        descriptor, temporary = tempfile.mkstemp(prefix="pre-push-", dir=hook_directory)
        try:
            with os.fdopen(descriptor, "wb") as target:
                os.fchmod(descriptor, 0o700)
                target.write(source.read_bytes())
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, hook_directory / "pre-push")
        except BaseException:
            try:
                Path(temporary).unlink()
            except OSError:
                pass
            raise
    except OSError as error:
        raise PreflightError("could not install the shared publication hook") from error
    for key, value in (
        ("core.hooksPath", str(hook_directory)),
        (POLICY_GIT_CONFIG, str(policy)),
        ("hub.publishPython", str(interpreter)),
    ):
        result = _run(["git", "config", "--local", key, value], cwd=root)
        if result.returncode != 0:
            raise PreflightError("could not install the repository publication hook")
    _configure_all_worktree_hooks(root, hook_directory)


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
        if validator.returncode != 0:
            sys.stdout.write(validator.stdout)
            sys.stderr.write(validator.stderr)
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
