#!/usr/bin/env python3
"""Independent switcher for prepared releases; no Hub imports or downloads."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

_ID = re.compile(r"[a-z][a-z0-9-]{0,47}")
_VERSION = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._+-]{0,127}")
_UNIT = re.compile(r"[a-zA-Z0-9@._-]+\.service")
_FINISHED = {"applied", "rolled_back"}
_LIMIT = 1024 * 1024


class Refused(ValueError):
    """No new service or link changes are authorized."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise Refused(message)


def encoded(document: object) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def digest(document: object) -> str:
    return hashlib.sha256(encoded(document)).hexdigest()


def canonical(raw: object) -> Path:
    require(isinstance(raw, str) and raw, "path must be absolute and canonical")
    path = Path(str(raw))
    require(path.is_absolute() and ".." not in path.parts, "path must be absolute and canonical")
    require(path.resolve() == path, "path must have no symlinked ancestors")
    return path


def private(path: Path, *, directory: bool = False) -> None:
    info = path.lstat()
    require(
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
        "private input has wrong file type",
    )
    require(
        info.st_uid == os.getuid() and not info.st_mode & 0o077,
        "private input must belong to current user with mode 0600/0700",
    )


def read_json(path: Path) -> dict[str, Any]:
    private(path)
    require(path.stat().st_size <= _LIMIT, "private input is too large")
    value = json.loads(path.read_bytes())
    require(isinstance(value, dict), "private input must be a JSON object")
    return value


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path: Path, document: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".stack-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded(document) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def commands(value: object) -> None:
    if not isinstance(value, list) or not value:
        raise Refused("each gate requires nonempty argv lists")
    for argv in value:
        require(
            isinstance(argv, list)
            and argv
            and all(isinstance(arg, str) and arg and "\x00" not in arg for arg in argv),
            "gate must be a nonempty argv list",
        )
        require(argv[0].startswith(("/", "{")), "gate executable must be absolute")


def validate_manifest(document: dict[str, Any]) -> dict[str, Any]:
    require(
        set(document) == {"schema_version", "state_dir", "components", "groups"}
        and type(document["schema_version"]) is int
        and document["schema_version"] == 1,
        "unsupported manifest fields or schema",
    )
    state = canonical(document["state_dir"])
    require(len(state.parts) >= 4, "state directory is too broad")
    components, groups = document["components"], document["groups"]
    require(
        isinstance(components, list) and 0 < len(components) <= 32,
        "manifest requires 1..32 components",
    )
    require(isinstance(groups, list) and 0 < len(groups) <= 32, "manifest requires groups")
    ids, links, roots, group_ids, units = set(), set(), [], set(), set()
    for group in groups:
        require(
            isinstance(group, dict)
            and set(group) == {"id", "units", "preflight", "check", "health"},
            "invalid group fields",
        )
        name = group["id"]
        require(
            isinstance(name, str) and _ID.fullmatch(name) and name not in group_ids,
            "invalid or duplicate group",
        )
        group_ids.add(name)
        require(isinstance(group["units"], list) and group["units"], "group requires units")
        for unit in group["units"]:
            require(
                isinstance(unit, str) and _UNIT.fullmatch(unit) and unit not in units,
                "shared units must belong to a single group",
            )
            units.add(unit)
        for phase in ("preflight", "check", "health"):
            commands(group[phase])
    for item in components:
        require(
            isinstance(item, dict) and set(item) == {"id", "releases", "link", "target", "group"},
            "invalid component fields",
        )
        name = item["id"]
        require(
            isinstance(name, str) and _ID.fullmatch(name) and name not in ids,
            "invalid or duplicate component",
        )
        require(name != "hermes", "Hermes self-update requires an independent watchdog")
        ids.add(name)
        require(item["group"] in group_ids, "component names an unknown group")
        version = item["target"]
        require(
            isinstance(version, str) and _VERSION.fullmatch(version) and version not in {".", ".."},
            "invalid version directory",
        )
        root = canonical(item["releases"])
        require(root.is_dir() and len(root.parts) >= 4, "release directory is missing or too broad")
        link = Path(item["link"])
        require(
            link.is_absolute() and ".." not in link.parts and link.name != "",
            "link path must be absolute",
        )
        canonical(str(link.parent))
        require(link not in links, "duplicate link")
        links.add(link)
        roots.append(root)
    require(group_ids == {item["group"] for item in components}, "unused group")
    for root in roots:
        require(
            all(link != root and root not in link.parents for link in links),
            "links must be outside release directories",
        )
        require(
            state != root and state not in root.parents and root not in state.parents,
            "state and releases must be separate",
        )
        require(
            all(
                other == root or (other not in root.parents and root not in other.parents)
                for other in roots
            ),
            "release roots must not overlap",
        )
    require(len(set(roots)) == len(roots), "components require separate release directories")
    return document


def load_manifest(path: Path) -> dict[str, Any]:
    return validate_manifest(read_json(canonical(str(path))))


def release(root: Path, path: Path) -> Path:
    require(
        path.is_dir() and path.parent == root and path.resolve() == path,
        "release must be a canonical direct child of its version directory",
    )
    return path


def link_target(item: dict[str, Any]) -> Path:
    link, root = Path(item["link"]), Path(item["releases"])
    require(link.is_symlink(), "current must already be a version-directory symlink")
    target = link.resolve()
    require(target.parent == root, "current link escapes version directory")
    return target


def current(item: dict[str, Any]) -> Path:
    return release(Path(item["releases"]), link_target(item))


def tree_entries(root: Path) -> list[Path]:
    """Enumerate every entry or refuse, without following directory links."""
    paths, pending = [root], [root]
    while pending:
        directory = pending.pop()
        require(len(directory.relative_to(root).parts) < 128, "release exceeds depth limit")
        for path in directory.iterdir():
            require(len(paths) < 100_000, "release exceeds entry limit")
            paths.append(path)
            if stat.S_ISDIR(path.lstat().st_mode):
                pending.append(path)
    return [root, *sorted(paths[1:])]


def tree_digest(root: Path) -> str:
    """Bind prepared bytes; external symlinks are limited to immutable OS files."""
    result = hashlib.sha256()
    for path in tree_entries(root):
        info = path.lstat()
        record: list[Any] = [str(path.relative_to(root)), stat.S_IMODE(info.st_mode)]
        if path.is_symlink():
            literal = os.readlink(path)
            raw = Path(literal)
            target = path.resolve(strict=True)
            lexical = Path(os.path.normpath(path.parent / raw))
            internal = (lexical == root or root in lexical.parents) and (
                target == root or root in target.parents
            )
            if internal:
                require(
                    not raw.is_absolute() or raw.is_relative_to(root),
                    "release symlink escapes trusted code",
                )
                cursor = root if raw.is_absolute() else path.parent
                parts = raw.relative_to(root).parts if raw.is_absolute() else raw.parts
                for part in parts:
                    cursor = (cursor / part).resolve(strict=True)
                    require(
                        cursor == root or root in cursor.parents,
                        "release symlink escapes trusted code",
                    )
            else:
                require(
                    raw.is_absolute()
                    and Path("/usr") in raw.parents
                    and Path("/usr") in target.parents,
                    "release symlink escapes trusted code",
                )
                for parent in (target, *target.parents):
                    details = parent.stat()
                    require(
                        details.st_uid == 0
                        and not details.st_mode & 0o022
                        and not os.access(parent, os.W_OK),
                        "external runtime must be immutable OS code",
                    )
            record.extend(("link", literal))
        elif path.is_file():
            require(not os.access(path, os.W_OK), "release files must be sealed read-only")
            require(info.st_nlink == 1, "release hardlinks are unsupported")
            content = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    content.update(chunk)
            record.extend(("file", content.hexdigest()))
        else:
            require(path.is_dir(), "release contains a special file")
            require(not os.access(path, os.W_OK), "release directories must be sealed read-only")
            record.append("directory")
        result.update(encoded(record) + b"\n")
    return result.hexdigest()


def record_path(document: dict[str, Any]) -> Path:
    return Path(document["state_dir"]) / "switch.json"


def read_record(document: dict[str, Any]) -> dict[str, Any] | None:
    path = record_path(document)
    if path.exists() or path.is_symlink():
        private(path.parent, directory=True)
        record = read_json(path)
        require(
            set(record) == {"schema_version", "status", "plan"}
            and record["schema_version"] == 1
            and record["status"] in _FINISHED | {"applying", "rolling_back", "recovery_failed"},
            "invalid switch record",
        )
        plan = record["plan"]
        require(
            isinstance(plan, dict)
            and set(plan) == {"manifest", "entries", "previous", "digest"}
            and plan["digest"]
            == digest({key: value for key, value in plan.items() if key != "digest"}),
            "switch record digest mismatch",
        )
        manifest = validate_manifest(plan["manifest"])
        require(manifest["state_dir"] == document["state_dir"], "switch record location mismatch")
        entries = plan["entries"]
        require(
            isinstance(entries, list) and len(entries) == len(manifest["components"]),
            "invalid switch entries",
        )
        for entry, item in zip(entries, manifest["components"], strict=True):
            require(
                set(entry) == set(item) | {"before", "after", "before_digest", "after_digest"}
                and all(entry[key] == value for key, value in item.items()),
                "switch entry does not match manifest",
            )
            root = Path(item["releases"])
            require(
                Path(entry["before"]).parent == root
                and entry["after"] == str(root / item["target"]),
                "invalid switch targets",
            )
        return record
    return None


def build_plan(document: dict[str, Any]) -> dict[str, Any]:
    previous = read_record(document)
    entries = []
    for item in document["components"]:
        before = current(item)
        after = release(Path(item["releases"]), Path(item["releases"]) / item["target"])
        entries.append(
            {
                **item,
                "before": str(before),
                "after": str(after),
                "before_digest": tree_digest(before),
                "after_digest": tree_digest(after),
            }
        )
    plan = {"manifest": document, "entries": entries, "previous": digest(previous)}
    return {**plan, "digest": digest(plan)}


@contextmanager
def locked(document: dict[str, Any]) -> Iterator[None]:
    state = canonical(document["state_dir"])
    if not state.exists():
        state.mkdir(mode=0o700, parents=True)
        fsync_directory(state.parent)
    private(state, directory=True)
    descriptor = os.open(state / "lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        private(state / "lock")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("another switch or rollback is running") from None
        yield
    finally:
        os.close(descriptor)


def run_command(argv: list[str]) -> None:
    try:
        subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("service action or release gate failed") from None


def gates(
    plan: dict[str, Any], phase: str, direction: str, groups: list[dict[str, Any]] | None = None
) -> None:
    directories = {entry["id"]: entry[direction] for entry in plan["entries"]}
    directories.update(
        {
            entry["id"] + "_" + side: entry[side]
            for entry in plan["entries"]
            for side in ("before", "after")
        }
    )
    for group in groups if groups is not None else plan["manifest"]["groups"]:
        for command in group[phase]:
            argv = []
            for argument in command:
                for name, directory in directories.items():
                    argument = argument.replace("{" + name + "}", directory)
                require(
                    not re.search(r"\{[a-zA-Z][\w-]*\}", argument), "unknown directory placeholder"
                )
                argv.append(argument)
            run_command(argv)


def flip_link(link: Path, target: Path) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".current-", dir=link.parent)
    os.close(descriptor)
    Path(temporary).unlink()
    try:
        Path(temporary).symlink_to(target, target_is_directory=True)
        os.replace(temporary, link)
        fsync_directory(link.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_recovery(plan: dict[str, Any]) -> None:
    load_document = plan["manifest"]
    for item in plan["entries"]:
        require(
            str(link_target(item)) in {item["before"], item["after"]},
            "current link changed outside this switch",
        )
        before = release(Path(item["releases"]), Path(item["before"]))
        require(tree_digest(before) == item["before_digest"], "rollback artifact changed")
    require(record_path(load_document).parent.is_dir(), "switch state is missing")


def activate(plan: dict[str, Any], direction: str) -> None:
    groups = plan["manifest"]["groups"]
    if direction == "before":
        groups = list(reversed(groups))
    for group in groups:
        run_command(["systemctl", "--user", "stop", *reversed(group["units"])])
        for entry in plan["entries"]:
            if entry["group"] == group["id"]:
                # A process may have died after an earlier link flip.
                require(
                    str(link_target(entry)) in {entry["before"], entry["after"]},
                    "link changed during switch",
                )
                flip_link(Path(entry["link"]), Path(entry[direction]))
        run_command(["systemctl", "--user", "start", *group["units"]])
        gates(plan, "health", direction, [group])


def rollback(record: dict[str, Any]) -> dict[str, Any]:
    plan = record["plan"]
    validate_recovery(plan)
    if record["status"] == "rolled_back":
        return record
    gates(plan, "preflight", "before")
    gates(plan, "check", "before")
    validate_recovery(plan)
    record["status"] = "rolling_back"
    path = record_path(plan["manifest"])
    write_json(path, record)
    try:
        activate(plan, "before")
        record["status"] = "rolled_back"
        write_json(path, record)
    except (OSError, RuntimeError, Refused):
        record["status"] = "recovery_failed"
        write_json(path, record)
        raise
    return record


def apply(path: Path, document: dict[str, Any], confirmation: str) -> dict[str, Any]:
    previous = read_record(document)
    require(
        previous is None or previous.get("status") in _FINISHED,
        "unfinished switch: rollback its recorded plan first",
    )
    plan = build_plan(document)
    require(plan["digest"] == confirmation, "plan changed; inspect a new plan")
    require(
        any(item["before"] != item["after"] for item in plan["entries"]), "no versions to switch"
    )
    try:
        gates(plan, "preflight", "before")
        gates(plan, "check", "after")
    except RuntimeError:
        raise Refused(
            "release gate failed before switching; links and services unchanged"
        ) from None
    gates(plan, "preflight", "before")
    require(
        load_manifest(path) == document and build_plan(document) == plan,
        "manifest, artifacts or links changed while checking",
    )
    record = {"schema_version": 1, "status": "applying", "plan": plan}
    write_json(record_path(document), record)
    try:
        activate(plan, "after")
        record["status"] = "applied"
        write_json(record_path(document), record)
    except (OSError, RuntimeError, Refused):
        try:
            rollback(record)
        except (OSError, RuntimeError, Refused):
            record["status"] = "recovery_failed"
            write_json(record_path(document), record)
        return {"ok": False, "status": record["status"], "digest": plan["digest"]}
    return {"ok": True, "status": "applied", "digest": plan["digest"]}


def launch(document: dict[str, Any], name: str, argv: list[str]) -> None:
    item = next((item for item in document["components"] if item["id"] == name), None)
    if item is None:
        raise Refused("unknown component")
    directory = current(item)
    require(
        argv and not Path(argv[0]).is_absolute() and ".." not in Path(argv[0]).parts,
        "launch executable must be relative to the resolved release",
    )
    arguments = [argument.replace("{dir}", str(directory)) for argument in argv]
    arguments[0] = str(directory / arguments[0])
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.execv(arguments[0], arguments)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("plan", help="inspect prepared versions without running commands")
    for action in ("apply", "rollback"):
        command = subparsers.add_parser(action)
        command.add_argument("--confirm", required=True, help="exact owner-confirmed plan digest")
    command = subparsers.add_parser(
        "launch", help="resolve current once, then exec a canonical release"
    )
    command.add_argument("component")
    command.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        document = load_manifest(args.manifest)
        if args.action == "plan":
            report = {"ok": True, **build_plan(document)}
        elif args.action == "launch":
            launch(document, args.component, args.argv)
            raise AssertionError("exec returned")
        else:
            with locked(document):
                if args.action == "apply":
                    report = apply(args.manifest, document, args.confirm)
                else:
                    record = read_record(document)
                    if record is None:
                        raise Refused("no recorded switch")
                    require(record["plan"]["digest"] == args.confirm, "wrong rollback confirmation")
                    rollback(record)
                    report = {"ok": True, "status": record["status"], "digest": args.confirm}
        print(json.dumps(report, sort_keys=True))
        return 0 if report["ok"] else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        message = (
            str(error) if isinstance(error, Refused) else "invalid or unavailable private input"
        )
        print(json.dumps({"ok": False, "status": "refused", "reason": message}))
        return 2
    except RuntimeError:
        print(
            json.dumps(
                {
                    "ok": False,
                    "status": "recovery_failed",
                    "reason": "release gate or service action failed; inspect switch record",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
