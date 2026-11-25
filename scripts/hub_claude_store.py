"""Claude CLI store for unconfined Hub workers (ADR 0113); standard library only.

One read-only object per digest, immutable selections and one ``current``
pointer. Callers hold the deploy lock and inject the release gate, the
post-switch check, the clock and the manifest fetch, so every decision here is
deterministic and testable without a network or a CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable

SCHEMA = 1
SOAK_SECONDS = 24 * 3600
# Nightly runs start minutes apart; last night's candidate must be eligible tonight.
JITTER_SECONDS = 3600
FRESH_CANDIDATE_SECONDS = 3 * 24 * 3600
KEPT_SELECTIONS = 4
KEPT_RESULTS = 100
MANIFEST_BASE = "https://downloads.claude.ai/claude-code-releases"
PLATFORM = "linux-x64"
MAX_MANIFEST = 64 * 1024
GATE_TIMEOUT = 300
_HEX = re.compile(r"[0-9a-f]{64}")
_VERSION = re.compile(r"([0-9]{1,4})\.([0-9]{1,4})\.([0-9]{1,6})")
_SELECTION = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}(?:-[0-9]{1,3})?")
_DIRS = ("objects", "candidates", "pairs", "quarantine", "selections", "results")

Gate = Callable[[Path, str, "Path | None"], dict[str, Any]]
PostCheck = Callable[[Path, str], bool]
Fetch = Callable[[str], bytes]

EXIT = {
    "promoted": 0,
    "rolled_back": 0,
    "current": 0,
    "skipped": 0,
    "gate_failed": 1,
    "failed_rolled_back": 1,
    "needs_inspection": 2,
    "refused": 3,
}


class StoreRefused(Exception):
    """Nothing changed; exit code 3."""


class NeedsInspection(Exception):
    """The state cannot be decided; nothing more is changed (exit code 2)."""


def need(condition: object, message: str) -> None:
    if not condition:
        raise StoreRefused(message)


def version_key(version: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(version)
    need(match is not None, "invalid version")
    assert match is not None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def stable_path(store: Path) -> Path:
    return store / "current" / "claude"


def _stamp(now: float) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_json(path: Path, value: object) -> None:
    """Atomically replace one private JSON file."""
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _read_json(path: Path) -> dict[str, Any]:
    info = path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid(), "unsafe store record")
    value = json.loads(path.read_text(encoding="utf-8"))
    need(isinstance(value, dict), "invalid store record")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def open_store(store: Path) -> Path:
    """Validate the store root and create its fixed subdirectories."""
    need(
        store.is_absolute() and str(store) == os.path.normpath(store),
        "store path must be canonical",
    )
    need(str(store.resolve()) == str(store), "store path must be canonical")
    info = store.lstat()
    need(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) & 0o022 == 0,
        "store must be a user-owned directory without group or other write",
    )
    for name in _DIRS:
        path = store / name
        if not path.exists():
            path.mkdir(mode=0o700)
        need(path.is_dir() and not path.is_symlink(), "unsafe store directory: " + name)
    return store


def object_path(store: Path, digest: str) -> Path:
    need(_HEX.fullmatch(digest) is not None, "invalid digest")
    return store / "objects" / digest / "claude"


def fetch_url(url: str) -> bytes:
    need(url.startswith(MANIFEST_BASE + "/"), "unexpected manifest origin")
    request = urllib.request.Request(url, headers={"User-Agent": "agents-projects-hub"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS origin
        need(response.geturl() == url, "manifest redirected")
        data: bytes = response.read(MAX_MANIFEST + 1)
    need(len(data) <= MAX_MANIFEST, "manifest too large")
    return data


def publisher_entry(version: str, fetch: Fetch) -> dict[str, Any]:
    """Return the publisher's checksum and size for this platform and version."""
    version_key(version)
    url = MANIFEST_BASE + "/" + version + "/manifest.json"
    try:
        manifest = json.loads(fetch(url))
    except StoreRefused:
        raise
    except Exception:
        raise StoreRefused("publisher manifest unavailable") from None
    need(
        isinstance(manifest, dict) and manifest.get("version") == version,
        "manifest version mismatch",
    )
    entry = (manifest.get("platforms") or {}).get(PLATFORM)
    need(
        isinstance(entry, dict)
        and isinstance(entry.get("checksum"), str)
        and _HEX.fullmatch(entry["checksum"])
        and type(entry.get("size")) is int,
        "manifest has no entry for this platform",
    )
    return {"url": url, "sha256": entry["checksum"], "size": entry["size"]}


def _copy_in(store: Path, source: Path) -> tuple[Path, str, int]:
    """Copy one regular file into a private temporary file and hash the copy."""
    resolved = source.resolve(strict=True)
    info = resolved.stat()
    need(stat.S_ISREG(info.st_mode), "candidate is not a regular file")
    need(info.st_uid in {0, os.geteuid()} and not info.st_mode & 0o022, "unsafe candidate file")
    temporary = store / "objects" / (".incoming-%d" % os.getpid())
    if temporary.exists():
        temporary.unlink()
    digest = hashlib.sha256()
    size = 0
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with open(resolved, "rb") as reader, os.fdopen(fd, "wb") as writer:
        for block in iter(lambda: reader.read(1 << 20), b""):
            digest.update(block)
            size += len(block)
            writer.write(block)
        writer.flush()
        os.fsync(writer.fileno())
    return temporary, digest.hexdigest(), size


def _publish(store: Path, temporary: Path, digest: str) -> None:
    target = object_path(store, digest)
    if target.exists():
        os.unlink(temporary)
        need(target.is_file() and sha256_file(target) == digest, "stored object is corrupt")
        return
    directory = target.parent
    if directory.exists():
        need(not any(directory.iterdir()), "unexpected object directory content")
    else:
        directory.mkdir(mode=0o700)
    os.chmod(temporary, 0o555)
    os.rename(temporary, target)
    os.chmod(directory, 0o555)
    _fsync_dir(directory)
    _fsync_dir(directory.parent)


def admit(
    store: Path, source: Path, version: str, *, now: float, fetch: Fetch = fetch_url
) -> dict[str, Any]:
    """Admit a host-installed candidate only when it matches the publisher manifest."""
    version_key(version)
    temporary, digest, size = _copy_in(store, source)
    try:
        entry = publisher_entry(version, fetch)
        need(
            entry["sha256"] == digest and entry["size"] == size, "candidate does not match manifest"
        )
    except BaseException:
        os.unlink(temporary)
        raise
    receipt_path = store / "candidates" / (digest + ".json")
    if receipt_path.exists():
        os.unlink(temporary)
        receipt = _read_json(receipt_path)
        need(receipt.get("version") == version, "digest admitted under another version")
        need(object_path(store, digest).is_file(), "admitted object is missing")
        return receipt
    _publish(store, temporary, digest)
    receipt = {
        "schema": SCHEMA,
        "version": version,
        "sha256": digest,
        "size": size,
        "source": "publisher-manifest",
        "manifest_url": entry["url"],
        "admitted_at": int(now),
    }
    _write_json(receipt_path, receipt)
    return receipt


def seed(store: Path, source: Path, version: str, origin: str, *, now: float) -> dict[str, Any]:
    """Admit the binary sealed in the active release; it is what production runs today."""
    version_key(version)
    temporary, digest, size = _copy_in(store, source)
    receipt_path = store / "candidates" / (digest + ".json")
    if receipt_path.exists():
        os.unlink(temporary)
        return _read_json(receipt_path)
    _publish(store, temporary, digest)
    receipt = {
        "schema": SCHEMA,
        "version": version,
        "sha256": digest,
        "size": size,
        "source": "sealed-release:" + origin,
        "admitted_at": int(now),
    }
    _write_json(receipt_path, receipt)
    return receipt


# ---------------------------------------------------------------------------
# Selections and the current pointer


def selection(store: Path, identifier: str) -> dict[str, Any]:
    need(_SELECTION.fullmatch(identifier) is not None, "invalid selection id")
    directory = store / "selections" / identifier
    record = _read_json(directory / "selection.json")
    digest = record.get("sha256")
    need(
        record.get("id") == identifier and isinstance(digest, str) and _HEX.fullmatch(digest),
        "invalid selection record",
    )
    link = directory / "claude"
    need(
        link.is_symlink() and os.readlink(link) == "../../objects/" + digest + "/claude",
        "selection link mismatch",
    )
    return record


def current_id(store: Path) -> str | None:
    pointer = store / "current"
    if not pointer.exists() and not pointer.is_symlink():
        return None
    need(pointer.is_symlink(), "current is not a link")
    target = os.readlink(pointer)
    need(target.startswith("selections/"), "current points outside selections")
    return target.removeprefix("selections/")


def current(store: Path) -> dict[str, Any] | None:
    identifier = current_id(store)
    return None if identifier is None else selection(store, identifier)


def write_selection(
    store: Path,
    candidate: dict[str, Any],
    *,
    previous: str | None,
    reason: str,
    hub_git_sha: str,
    now: float,
) -> str:
    digest = candidate["sha256"]
    need(object_path(store, digest).is_file(), "selected object is missing")
    base = _stamp(now) + "-" + digest[:12]
    identifier = base
    for index in range(1, 1000):
        if not (store / "selections" / identifier).exists():
            break
        identifier = base + "-" + str(index)
    directory = store / "selections" / identifier
    staging = store / "selections" / ("." + identifier)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(mode=0o700)
    record = {
        "schema": SCHEMA,
        "id": identifier,
        "component": "claude",
        "version": candidate["version"],
        "sha256": digest,
        "previous": previous,
        "reason": reason,
        "hub_git_sha": hub_git_sha,
        "selected_at": int(now),
    }
    _write_json(staging / "selection.json", record)
    os.symlink("../../objects/" + digest + "/claude", staging / "claude")
    os.chmod(staging / "selection.json", 0o444)
    _fsync_dir(staging)
    os.rename(staging, directory)
    os.chmod(directory, 0o555)
    _fsync_dir(directory.parent)
    return identifier


def _point(store: Path, identifier: str) -> None:
    temporary = store / ".current.tmp"
    if temporary.exists() or temporary.is_symlink():
        temporary.unlink()
    os.symlink("selections/" + identifier, temporary)
    os.replace(temporary, store / "current")
    _fsync_dir(store)


def switch(store: Path, identifier: str, *, now: float) -> None:
    """Record the transaction, then move ``current`` with one rename."""
    selection(store, identifier)
    _write_json(
        store / "pending.json", {"from": current_id(store), "to": identifier, "at": int(now)}
    )
    _point(store, identifier)


def finish(store: Path) -> None:
    pending = store / "pending.json"
    if pending.exists():
        pending.unlink()
        _fsync_dir(store)


def recover(store: Path, post_check: PostCheck) -> str | None:
    """Complete or revert an unfinished switch before anything else."""
    path = store / "pending.json"
    if not path.exists():
        return None
    pending = _read_json(path)
    source, target, active = pending.get("from"), pending.get("to"), current_id(store)
    if active == source:
        finish(store)
        return "switch_not_started"
    if active != target:
        raise NeedsInspection("current matches neither side of the unfinished switch")
    if _checked(store, target, post_check):
        finish(store)
        return "switch_completed"
    if source is None:
        raise NeedsInspection("unfinished first switch failed its check")
    _point(store, source)
    if _checked(store, source, post_check):
        finish(store)
        return "switch_reverted"
    raise NeedsInspection("neither side of the unfinished switch passes its check")


def _checked(store: Path, identifier: str, post_check: PostCheck) -> bool:
    try:
        record = selection(store, identifier)
        path = stable_path(store)
        return (
            os.path.realpath(path) == str(object_path(store, record["sha256"]))
            and sha256_file(path) == record["sha256"]
            and post_check(path, record["version"])
        )
    except (OSError, ValueError, StoreRefused):
        return False


# ---------------------------------------------------------------------------
# Pair evidence, quarantine and candidates


def _pair_name(hub_git_sha: str, digest: str) -> str:
    need(re.fullmatch(r"[0-9a-f]{40}", hub_git_sha) is not None, "invalid Hub revision")
    need(_HEX.fullmatch(digest) is not None, "invalid digest")
    return hub_git_sha + "-" + digest + ".json"


def has_pair(store: Path, hub_git_sha: str, digest: str) -> bool:
    return (store / "pairs" / _pair_name(hub_git_sha, digest)).is_file()


def quarantined(store: Path, hub_git_sha: str, digest: str) -> bool:
    return (store / "quarantine" / _pair_name(hub_git_sha, digest)).is_file()


def _record(store: Path, kind: str, hub_git_sha: str, digest: str, value: dict[str, Any]) -> None:
    _write_json(store / kind / _pair_name(hub_git_sha, digest), value)


def run_pair_gate(
    store: Path,
    gate: Gate,
    hub_git_sha: str,
    candidate: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    now: float,
) -> dict[str, Any]:
    """Run the release gate once and record its verdict for the pair."""
    previous_path = (
        object_path(store, previous["sha256"])
        if previous is not None and previous["sha256"] != candidate["sha256"]
        else None
    )
    report = gate(object_path(store, candidate["sha256"]), candidate["version"], previous_path)
    passed = report.get("passed") is True
    summary = {
        "schema": SCHEMA,
        "hub_git_sha": hub_git_sha,
        "sha256": candidate["sha256"],
        "version": candidate["version"],
        "previous_sha256": None if previous_path is None else previous["sha256"],  # type: ignore[index]
        "gate_version": report.get("gate_version"),
        "checks": report.get("checks"),
        "failure": report.get("failure"),
        "at": int(now),
    }
    _record(store, "pairs" if passed else "quarantine", hub_git_sha, candidate["sha256"], summary)
    return summary | {"passed": passed}


def ensure_pair(store: Path, gate: Gate, hub_git_sha: str, *, now: float) -> dict[str, Any]:
    """Require gate evidence for a Hub release and the active CLI (deploy path)."""
    active = current(store)
    need(active is not None, "Claude CLI store has no active selection")
    assert active is not None
    if has_pair(store, hub_git_sha, active["sha256"]):
        return active
    need(
        not quarantined(store, hub_git_sha, active["sha256"]),
        "release failed the gate for the active Claude CLI",
    )
    result = run_pair_gate(store, gate, hub_git_sha, active, None, now=now)
    need(result["passed"], "release failed the gate for the active Claude CLI")
    return active


def candidates(store: Path) -> list[dict[str, Any]]:
    result = []
    for path in sorted((store / "candidates").glob("*.json")):
        record = _read_json(path)
        if (
            isinstance(record.get("sha256"), str)
            and path.name == record["sha256"] + ".json"
            and isinstance(record.get("version"), str)
            and _VERSION.fullmatch(record["version"])
            and type(record.get("admitted_at")) is int
            and object_path(store, record["sha256"]).is_file()
        ):
            result.append(record)
    return sorted(result, key=lambda item: version_key(item["version"]), reverse=True)


def eligible_at(record: dict[str, Any]) -> int:
    return int(record["admitted_at"]) + SOAK_SECONDS - JITTER_SECONDS


# ---------------------------------------------------------------------------
# Operations


def _result(outcome: str, **extra: Any) -> dict[str, Any]:
    return {"component": "claude", "outcome": outcome, **extra}


def _brief(record: dict[str, Any] | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {key: record.get(key) for key in ("id", "version", "sha256", "reason", "selected_at")}


def promote(
    store: Path,
    *,
    hub_git_sha: str,
    gate: Gate,
    post_check: PostCheck,
    now: float,
) -> dict[str, Any]:
    """Promote the newest candidate that soaked and passed the gate for this release."""
    note = recover(store, post_check)
    active = current(store)
    need(active is not None, "Claude CLI store has no active selection")
    assert active is not None
    newer = [
        c for c in candidates(store) if version_key(c["version"]) > version_key(active["version"])
    ]
    usable = [c for c in newer if not quarantined(store, hub_git_sha, c["sha256"])]
    ready = [c for c in usable if eligible_at(c) <= now]
    rejected: list[dict[str, Any]] = []
    for candidate in ready:
        if not has_pair(store, hub_git_sha, candidate["sha256"]):
            gated = run_pair_gate(store, gate, hub_git_sha, candidate, active, now=now)
            if not gated["passed"]:
                rejected.append({"version": candidate["version"], "failure": gated["failure"]})
                continue
        identifier = write_selection(
            store,
            candidate,
            previous=active["id"],
            reason="promote",
            hub_git_sha=hub_git_sha,
            now=now,
        )
        switch(store, identifier, now=now)
        if _checked(store, identifier, post_check):
            finish(store)
            prune(store, now=now)
            return _result(
                "promoted",
                active=_brief(selection(store, identifier)),
                previous=_brief(active),
                rejected=rejected,
                recovered=note,
            )
        _record(
            store,
            "quarantine",
            hub_git_sha,
            candidate["sha256"],
            {"schema": SCHEMA, "failure": "post_check_failed", "at": int(now)},
        )
        _point(store, active["id"])
        if _checked(store, active["id"], post_check):
            finish(store)
            return _result(
                "failed_rolled_back",
                active=_brief(active),
                failed=candidate["version"],
                rejected=rejected,
                recovered=note,
            )
        raise NeedsInspection("post-check failed for the candidate and for the previous selection")
    prune(store, now=now)
    if rejected:
        return _result("gate_failed", active=_brief(active), rejected=rejected, recovered=note)
    soaking = [c for c in usable if eligible_at(c) > now]
    if soaking:
        return _result(
            "skipped",
            active=_brief(active),
            reason="soaking",
            next_version=soaking[0]["version"],
            eligible_at=min(eligible_at(c) for c in soaking),
            recovered=note,
        )
    return _result("current", active=_brief(active), recovered=note)


def rollback(
    store: Path, *, hub_git_sha: str, gate: Gate, post_check: PostCheck, now: float
) -> dict[str, Any]:
    """Return to the previous selection; quarantine the digest that is left."""
    note = recover(store, post_check)
    active = current(store)
    need(active is not None, "Claude CLI store has no active selection")
    assert active is not None
    need(active.get("previous") is not None, "no previous selection")
    previous = selection(store, active["previous"])
    need(previous["sha256"] != active["sha256"], "previous selection has the same CLI")
    if not has_pair(store, hub_git_sha, previous["sha256"]):
        gated = run_pair_gate(store, gate, hub_git_sha, previous, active, now=now)
        need(gated["passed"], "previous Claude CLI fails the gate for the active release")
    target = {"version": previous["version"], "sha256": previous["sha256"]}
    identifier = write_selection(
        store, target, previous=active["id"], reason="rollback", hub_git_sha=hub_git_sha, now=now
    )
    switch(store, identifier, now=now)
    if _checked(store, identifier, post_check):
        finish(store)
        # Promotion must not bring the abandoned build back next night.
        _record(
            store,
            "quarantine",
            hub_git_sha,
            active["sha256"],
            {"schema": SCHEMA, "failure": "rolled_back", "at": int(now)},
        )
        return _result(
            "rolled_back",
            active=_brief(selection(store, identifier)),
            previous=_brief(active),
            recovered=note,
        )
    _point(store, active["id"])
    if _checked(store, active["id"], post_check):
        finish(store)
        return _result("failed_rolled_back", active=_brief(active), recovered=note)
    raise NeedsInspection("post-check failed for both selections")


def first_selection(
    store: Path, candidate: dict[str, Any], *, hub_git_sha: str, post_check: PostCheck, now: float
) -> dict[str, Any]:
    """Point an empty store at the seeded build (migration)."""
    need(current_id(store) is None, "store already has a selection")
    identifier = write_selection(
        store, candidate, previous=None, reason="seed", hub_git_sha=hub_git_sha, now=now
    )
    switch(store, identifier, now=now)
    if not _checked(store, identifier, post_check):
        raise NeedsInspection("seeded selection failed its check")
    finish(store)
    return selection(store, identifier)


def status(store: Path, *, hub_git_sha: str | None, now: float) -> dict[str, Any]:
    """Read-only view for the host tool, backup and monitor."""
    active = current(store)
    previous = selection(store, active["previous"]) if active and active.get("previous") else None
    listed = []
    for record in candidates(store):
        if active is not None and record["sha256"] == active["sha256"]:
            state = "active"
        elif active is not None and version_key(record["version"]) <= version_key(
            active["version"]
        ):
            state = "older"
        elif hub_git_sha is not None and quarantined(store, hub_git_sha, record["sha256"]):
            state = "quarantined"
        else:
            state = "eligible" if eligible_at(record) <= now else "soaking"
        listed.append(
            {
                "version": record["version"],
                "sha256": record["sha256"],
                "admitted_at": record["admitted_at"],
                "eligible_at": eligible_at(record),
                "source": record.get("source"),
                "state": state,
            }
        )
    return _result(
        "current" if active else "refused",
        store=str(store),
        executable=str(stable_path(store)),
        active=_brief(active),
        previous=_brief(previous),
        pending=(store / "pending.json").exists(),
        pair_verified=(
            None
            if active is None or hub_git_sha is None
            else has_pair(store, hub_git_sha, active["sha256"])
        ),
        candidates=listed,
    )


def _remove_tree(path: Path) -> None:
    for item in (path, *path.rglob("*")):
        if item.is_dir() and not item.is_symlink():
            os.chmod(item, 0o700)
    shutil.rmtree(path)


def prune(store: Path, *, now: float) -> list[str]:
    """Delete objects that no kept selection or fresh candidate references."""
    active = current(store)
    selections_dir = store / "selections"
    records = []
    for directory in selections_dir.iterdir():
        if directory.name.startswith("."):
            continue
        try:
            records.append(selection(store, directory.name))
        except (OSError, ValueError, StoreRefused):
            continue
    records.sort(key=lambda r: (r.get("selected_at", 0), r["id"]), reverse=True)
    kept_ids = {r["id"] for r in records[:KEPT_SELECTIONS]}
    if active is not None:
        kept_ids.add(active["id"])
        if active.get("previous"):
            kept_ids.add(active["previous"])
    kept = {r["sha256"] for r in records if r["id"] in kept_ids}
    for record in candidates(store):
        if now - record["admitted_at"] < FRESH_CANDIDATE_SECONDS or (
            active is not None and version_key(record["version"]) > version_key(active["version"])
        ):
            kept.add(record["sha256"])
    removed = []
    for record in records:
        if record["id"] not in kept_ids and record["sha256"] not in kept:
            _remove_tree(selections_dir / record["id"])
    for directory in (store / "objects").iterdir():
        if directory.name.startswith(".") or directory.name in kept:
            continue
        if _HEX.fullmatch(directory.name):
            _remove_tree(directory)
            removed.append(directory.name)
            receipt = store / "candidates" / (directory.name + ".json")
            if receipt.exists():
                receipt.unlink()
            for pair in (store / "pairs").glob("*-" + directory.name + ".json"):
                pair.unlink()
    results = sorted((store / "results").glob("*.json"))
    for old in results[:-KEPT_RESULTS]:
        old.unlink()
    return removed


def save_result(store: Path, result: dict[str, Any], *, now: float) -> Path:
    base = _stamp(now)
    path = store / "results" / (base + ".json")
    index = 1
    while path.exists():
        path = store / "results" / ("%s-%d.json" % (base, index))
        index += 1
    _write_json(path, result)
    return path


def in_window(window: str, now: float) -> bool:
    """``HH:MM-HH:MM`` in local time; a window may cross midnight."""
    match = re.fullmatch(r"([0-2][0-9]):([0-5][0-9])-([0-2][0-9]):([0-5][0-9])", window)
    need(match is not None, "invalid Claude promotion window")
    assert match is not None
    start = int(match.group(1)) * 60 + int(match.group(2))
    end = int(match.group(3)) * 60 + int(match.group(4))
    need(start < 24 * 60 and end < 24 * 60 and start != end, "invalid Claude promotion window")
    local = time.localtime(now)
    minute = local.tm_hour * 60 + local.tm_min
    return start <= minute < end if start < end else minute >= start or minute < end


# ---------------------------------------------------------------------------
# Release-bound gate and post-check


def release_gate(root: Path) -> Gate:
    """Run the gate shipped in one sealed Hub release."""

    def gate(candidate: Path, version: str, previous: Path | None) -> dict[str, Any]:
        argv = [
            str(root / "venv/bin/python"),
            "-I",
            "-B",
            "-m",
            "hermes_codex_router.claude_cli_gate",
            "--candidate",
            str(candidate),
            "--version",
            version,
        ]
        if previous is not None:
            argv += ["--previous", str(previous)]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=GATE_TIMEOUT,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            report = json.loads(completed.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise StoreRefused("release gate could not run") from None
        need(isinstance(report, dict), "release gate could not run")
        if completed.returncode == 3:
            raise StoreRefused("release gate could not run: " + str(report.get("failure")))
        report["passed"] = report.get("passed") is True and completed.returncode == 0
        return report

    return gate


def release_post_check(root: Path) -> PostCheck:
    """Version and advertised controls of the stable path, as a worker sees it."""

    def check(path: Path, version: str) -> bool:
        argv = [
            str(root / "venv/bin/python"),
            "-I",
            "-B",
            "-m",
            "hermes_codex_router.claude_cli_gate",
            "--post-check",
            "--candidate",
            str(path),
            "--version",
            version,
        ]
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            report = json.loads(completed.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        return (
            completed.returncode == 0 and isinstance(report, dict) and report.get("passed") is True
        )

    return check
