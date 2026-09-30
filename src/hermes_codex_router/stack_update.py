"""Deterministic update tool for the external agent stack (ADR 0048, stage 1).

Standard library only: a pinned copy of this file must keep working when a Hub
or Hermes release is broken. A private manifest describes each component; the
tool plans read-only, then stages, checks and switches one exact plan, and
rolls back one recorded switch. It never picks versions on its own and never
runs a check marked as live inference unless the caller explicitly allows it.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

MANIFEST_SCHEMA_VERSION = 1
PLAN_SCHEMA_VERSION = 1
RECORD_SCHEMA_VERSION = 1
MARKER_NAME = ".stack-update.json"
NOT_A_LINK = "!not-a-link"

_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")
_UNIT = re.compile(r"^[A-Za-z0-9@._-]{1,128}\.service$")
_NPM_PACKAGE = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
_GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RECORD_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_JSON_LIMIT = 4 * 1024 * 1024
_ARCHIVE_LIMIT = 1024 * 1024 * 1024
_OUTPUT_LIMIT = 64 * 1024
_UNIT_TIMEOUT_SECONDS = 120.0
_STAGE_TIMEOUT_SECONDS = 900.0

Runner = Callable[..., subprocess.CompletedProcess[str]]
Fetcher = Callable[[str, int], bytes]


class StackUpdateError(Exception):
    """A refused or failed stack operation; the message names no secret."""


class StackUpdateBusy(StackUpdateError):
    """Another apply or rollback holds the stack lock."""


@dataclass(frozen=True, slots=True)
class Source:
    kind: str
    package: str | None = None
    url: str | None = None
    checksums_url: str | None = None
    github_repo: str | None = None
    lifecycle_scripts: bool = False


@dataclass(frozen=True, slots=True)
class Check:
    phase: str
    argv: tuple[str, ...] = ()
    url: str | None = None
    expect: str | None = None
    timeout_seconds: float = 20.0
    inference: bool = False


@dataclass(frozen=True, slots=True)
class Component:
    component_id: str
    order: int
    link: Path
    versions_dir: Path
    version: str
    source: Source
    version_argv: tuple[str, ...]
    units: tuple[str, ...]
    checks: tuple[Check, ...]


@dataclass(frozen=True, slots=True)
class StackManifest:
    path: Path
    digest: str
    state_dir: Path
    npm: Path | None
    components: tuple[Component, ...]

    def component(self, component_id: str) -> Component:
        for component in self.components:
            if component.component_id == component_id:
                return component
        raise StackUpdateError(f"unknown component: {component_id}")


@dataclass(frozen=True, slots=True)
class Environment:
    """Injected side effects, so the lifecycle is testable offline."""

    run: Runner = subprocess.run
    fetch: Callable[[str, int], bytes] | None = None
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC)

    def get(self, url: str, limit: int) -> bytes:
        return (self.fetch or _http_get)(url, limit)


# Manifest -------------------------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise StackUpdateError(message)


def _keys(raw: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    _require(not unknown, f"{where}: unknown keys: {', '.join(unknown)}")


def _absolute(raw: object, where: str) -> Path:
    _require(isinstance(raw, str) and raw.startswith("/"), f"{where} must be an absolute path")
    path = Path(str(raw))
    _require(".." not in path.parts, f"{where} must not contain '..'")
    return path


def _https_template(raw: object, where: str) -> str:
    _require(isinstance(raw, str), f"{where} must be a string")
    text = str(raw)
    _require(text.startswith("https://"), f"{where} must be an https URL")
    _require(
        "{" not in text.replace("{version}", "") and "}" not in text.replace("{version}", ""),
        f"{where} may only use the {{version}} placeholder",
    )
    return text


def _argv(raw: object, where: str) -> tuple[str, ...]:
    _require(
        isinstance(raw, list) and bool(raw) and all(isinstance(item, str) for item in raw),
        f"{where} must be a non-empty list of strings",
    )
    argv = tuple(str(item) for item in cast(list[object], raw))
    for item in argv:
        stripped = item.replace("{dir}", "")
        _require("{" not in stripped and "}" not in stripped, f"{where} may only use {{dir}}")
    _require(
        argv[0].startswith("/") or argv[0].startswith("{dir}/"),
        f"{where} must start with an absolute executable or {{dir}}/",
    )
    return argv


def _parse_source(raw: object, where: str) -> Source:
    _require(isinstance(raw, dict), f"{where} must be an object")
    data = cast(dict[str, Any], raw)
    kind = data.get("kind")
    lifecycle = data.get("lifecycle_scripts", False)
    _require(isinstance(lifecycle, bool), f"{where}.lifecycle_scripts must be a boolean")
    if kind == "npm":
        _keys(data, {"kind", "package", "lifecycle_scripts"}, where)
        package = data.get("package")
        _require(
            isinstance(package, str) and bool(_NPM_PACKAGE.match(package)),
            f"{where}.package must be an npm package name",
        )
        return Source(kind="npm", package=str(package), lifecycle_scripts=bool(lifecycle))
    if kind == "archive":
        _keys(data, {"kind", "url", "checksums_url", "github_repo", "lifecycle_scripts"}, where)
        _require(not lifecycle, f"{where}: archives have no lifecycle scripts")
        url = _https_template(data.get("url"), f"{where}.url")
        checksums = data.get("checksums_url")
        repo = data.get("github_repo")
        if repo is not None:
            _require(
                isinstance(repo, str) and bool(_GITHUB_REPO.match(repo)),
                f"{where}.github_repo must be OWNER/NAME",
            )
        return Source(
            kind="archive",
            url=url,
            checksums_url=None
            if checksums is None
            else _https_template(checksums, f"{where}.checksums_url"),
            github_repo=None if repo is None else str(repo),
        )
    raise StackUpdateError(f"{where}.kind must be 'npm' or 'archive'")


def _parse_check(raw: object, where: str) -> Check:
    _require(isinstance(raw, dict), f"{where} must be an object")
    data = cast(dict[str, Any], raw)
    _keys(data, {"phase", "argv", "url", "expect", "timeout_seconds", "inference"}, where)
    phase = data.get("phase")
    _require(phase in {"staged", "live"}, f"{where}.phase must be 'staged' or 'live'")
    expect = data.get("expect")
    _require(expect is None or isinstance(expect, str), f"{where}.expect must be a string")
    timeout = data.get("timeout_seconds", 20)
    _require(
        isinstance(timeout, int | float) and not isinstance(timeout, bool) and 0 < timeout <= 600,
        f"{where}.timeout_seconds must be in (0, 600]",
    )
    inference = data.get("inference", False)
    _require(isinstance(inference, bool), f"{where}.inference must be a boolean")
    has_argv = "argv" in data
    has_url = "url" in data
    _require(has_argv != has_url, f"{where} needs exactly one of argv or url")
    if has_url:
        _require(phase == "live", f"{where}: a url check needs a running service (phase live)")
        url = data["url"]
        _require(isinstance(url, str), f"{where}.url must be a string")
        parsed = urllib.parse.urlsplit(str(url))
        _require(
            parsed.scheme in {"http", "https"} and parsed.hostname in _LOOPBACK_HOSTS,
            f"{where}.url must be a loopback http(s) URL",
        )
        return Check(
            phase="live",
            url=str(url),
            expect=expect,
            timeout_seconds=float(timeout),
            inference=bool(inference),
        )
    return Check(
        phase=str(phase),
        argv=_argv(data["argv"], f"{where}.argv"),
        expect=expect,
        timeout_seconds=float(timeout),
        inference=bool(inference),
    )


def _parse_component(raw: object, index: int) -> Component:
    where = f"components[{index}]"
    _require(isinstance(raw, dict), f"{where} must be an object")
    data = cast(dict[str, Any], raw)
    _keys(
        data,
        {
            "id",
            "order",
            "link",
            "versions_dir",
            "version",
            "source",
            "version_argv",
            "units",
            "checks",
            "watchdog",
        },
        where,
    )
    component_id = data.get("id")
    _require(
        isinstance(component_id, str) and bool(_ID.match(component_id)),
        f"{where}.id must match {_ID.pattern}",
    )
    where = f"component {component_id}"
    _require(
        data.get("watchdog", False) is False,
        f"{where}: watchdog-managed components (Hermes) arrive with ADR 0048 stage 4",
    )
    order = data.get("order")
    _require(
        isinstance(order, int) and not isinstance(order, bool) and 0 <= order <= 10_000,
        f"{where}.order must be an integer in [0, 10000]",
    )
    link = _absolute(data.get("link"), f"{where}.link")
    versions_dir = _absolute(data.get("versions_dir"), f"{where}.versions_dir")
    _require(
        link != versions_dir and versions_dir not in link.parents,
        f"{where}.link must be outside versions_dir",
    )
    version = data.get("version")
    _require(
        isinstance(version, str) and bool(_VERSION.match(version)),
        f"{where}.version must match {_VERSION.pattern}",
    )
    units = data.get("units", [])
    _require(
        isinstance(units, list)
        and all(isinstance(unit, str) and _UNIT.match(unit) for unit in units),
        f"{where}.units must be a list of .service names",
    )
    checks = data.get("checks", [])
    _require(isinstance(checks, list), f"{where}.checks must be a list")
    return Component(
        component_id=str(component_id),
        order=cast(int, order),
        link=link,
        versions_dir=versions_dir,
        version=str(version),
        source=_parse_source(data.get("source"), f"{where}.source"),
        version_argv=_argv(data.get("version_argv"), f"{where}.version_argv"),
        units=tuple(str(unit) for unit in cast(list[object], units)),
        checks=tuple(
            _parse_check(check, f"{where}.checks[{position}]")
            for position, check in enumerate(cast(list[object], checks))
        ),
    )


def parse_manifest(raw: bytes, *, path: Path) -> StackManifest:
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StackUpdateError("manifest is not valid JSON") from exc
    _require(isinstance(data, dict), "manifest must be a JSON object")
    _keys(data, {"schema_version", "state_dir", "npm", "components"}, "manifest")
    _require(
        data.get("schema_version") == MANIFEST_SCHEMA_VERSION,
        f"manifest schema_version must be {MANIFEST_SCHEMA_VERSION}",
    )
    state_dir = _absolute(data.get("state_dir"), "manifest.state_dir")
    npm_raw = data.get("npm")
    npm = None if npm_raw is None else _absolute(npm_raw, "manifest.npm")
    raw_components = data.get("components")
    _require(
        isinstance(raw_components, list) and bool(raw_components),
        "manifest.components must be a non-empty list",
    )
    components = tuple(
        sorted(
            (_parse_component(item, index) for index, item in enumerate(raw_components)),
            key=lambda component: component.order,
        )
    )
    ids = [component.component_id for component in components]
    _require(len(set(ids)) == len(ids), "component ids must be unique")
    orders = [component.order for component in components]
    _require(len(set(orders)) == len(orders), "component orders must be unique")
    links = [component.link for component in components]
    _require(len(set(links)) == len(links), "component links must be unique")
    if any(component.source.kind == "npm" for component in components):
        _require(npm is not None, "manifest.npm is required for npm sources")
    return StackManifest(
        path=path,
        digest=hashlib.sha256(raw).hexdigest(),
        state_dir=state_dir,
        npm=npm,
        components=components,
    )


def load_manifest(path: Path) -> StackManifest:
    """Load a private manifest; it drives command execution, so it must be private."""

    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise StackUpdateError("manifest not found") from exc
    _require(path.is_file(), "manifest must be a regular file")
    _require(info.st_uid == os.getuid(), "manifest must be owned by the current user")
    _require(info.st_mode & 0o022 == 0, "manifest must not be group- or world-writable")
    return parse_manifest(path.read_bytes(), path=path)


# Link state -----------------------------------------------------------------


def link_state(component: Component) -> str | None:
    """Return the raw link target, None when absent, or a marker for a non-link."""

    if os.path.islink(component.link):
        return os.readlink(component.link)
    if os.path.lexists(component.link):
        return NOT_A_LINK
    return None


def installed_version(component: Component) -> str | None:
    """Return the version directory the link selects, or None when unmanaged."""

    target = link_state(component)
    if target is None or target == NOT_A_LINK:
        return None
    path = Path(target)
    if not path.is_absolute():
        path = component.link.parent / path
    if path.parent != component.versions_dir or not _VERSION.match(path.name):
        return None
    return path.name


def _flip_link(link: Path, target: str | None) -> None:
    """Atomically point ``link`` at ``target`` or remove it when ``target`` is None."""

    if target is None:
        with contextlib.suppress(FileNotFoundError):
            if os.path.islink(link):
                link.unlink()
        return
    temporary = link.parent / f".{link.name}.stack-update-{secrets.token_hex(4)}"
    os.symlink(target, temporary)
    try:
        os.replace(temporary, link)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise


# Network --------------------------------------------------------------------


def _http_get(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "stack-update"})
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        data = response.read(limit + 1)
    if len(data) > limit:
        raise StackUpdateError("download exceeds its size limit")
    return data


def _json_from(env: Environment, url: str) -> Any:
    try:
        return json.loads(env.get(url, _JSON_LIMIT))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StackUpdateError("registry answer is not JSON") from exc


def _npm_metadata_url(package: str, suffix: str) -> str:
    return f"https://registry.npmjs.org/{urllib.parse.quote(package, safe='@')}/{suffix}"


def resolve_latest(component: Component, env: Environment) -> str:
    """Ask the component's configured source for its latest release (read-only)."""

    source = component.source
    if source.kind == "npm":
        data = _json_from(env, _npm_metadata_url(str(source.package), "latest"))
        version = data.get("version") if isinstance(data, dict) else None
    elif source.github_repo is not None:
        data = _json_from(env, f"https://api.github.com/repos/{source.github_repo}/releases/latest")
        tag = data.get("tag_name") if isinstance(data, dict) else None
        version = tag[1:] if isinstance(tag, str) and tag.startswith("v") else tag
    else:
        raise StackUpdateError(f"component {component.component_id} has no latest-release source")
    _require(
        isinstance(version, str) and bool(_VERSION.match(version)),
        f"component {component.component_id}: source reported no usable version",
    )
    return str(version)


def _archive_name(url: str) -> str:
    name = PurePosixPath(urllib.parse.urlsplit(url).path).name
    _require(bool(name), "archive URL has no file name")
    return name


def _published_digest(component: Component, version: str, env: Environment) -> dict[str, str]:
    """Record what the source publishes about the exact artifact, if anything."""

    source = component.source
    if source.kind == "npm":
        data = _json_from(env, _npm_metadata_url(str(source.package), version))
        dist = data.get("dist") if isinstance(data, dict) else None
        integrity = dist.get("integrity") if isinstance(dist, dict) else None
        _require(
            isinstance(integrity, str) and integrity.startswith("sha512-"),
            f"component {component.component_id}: registry has no integrity for {version}",
        )
        return {"integrity": str(integrity)}
    if source.checksums_url is None:
        return {}
    url = str(source.url).format(version=version)
    checksums = env.get(source.checksums_url.format(version=version), _JSON_LIMIT)
    name = _archive_name(url)
    for line in checksums.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and _SHA256.match(parts[0].lower()):
            return {"sha256": parts[0].lower()}
    raise StackUpdateError(f"component {component.component_id}: no published checksum for {name}")


# Plan -----------------------------------------------------------------------


def _canonical(data: Mapping[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def plan_digest(plan: Mapping[str, Any]) -> str:
    body = {key: value for key, value in plan.items() if key != "digest"}
    return hashlib.sha256(_canonical(body)).hexdigest()


def _source_summary(component: Component, version: str) -> dict[str, Any]:
    source = component.source
    if source.kind == "npm":
        return {
            "kind": "npm",
            "package": source.package,
            "lifecycle_scripts": source.lifecycle_scripts,
        }
    return {"kind": "archive", "url": str(source.url).format(version=version)}


def build_plan(
    manifest: StackManifest,
    targets: Mapping[str, str],
    env: Environment,
) -> dict[str, Any]:
    """Build a read-only plan bound to the manifest and every current link."""

    _require(bool(targets), "no target versions given")
    steps: list[dict[str, Any]] = []
    for component in manifest.components:
        if component.component_id not in targets:
            continue
        version = targets[component.component_id]
        _require(
            bool(_VERSION.match(version)),
            f"component {component.component_id}: invalid version {version!r}",
        )
        state = link_state(component)
        current = installed_version(component)
        _require(
            state is None or current is not None,
            f"component {component.component_id}: its link is not managed yet; "
            "move it into version directories first",
        )
        if current == version:
            continue
        steps.append(
            {
                "component": component.component_id,
                "from": current,
                "to": version,
                "source": _source_summary(component, version),
                "published": _published_digest(component, version, env),
            }
        )
    unknown = sorted(set(targets) - {c.component_id for c in manifest.components})
    _require(not unknown, f"unknown components: {', '.join(unknown)}")
    _require(bool(steps), "every requested component already runs its target version")
    plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "created_at": env.now().isoformat(),
        "manifest_digest": manifest.digest,
        "links": {c.component_id: link_state(c) for c in manifest.components},
        "steps": steps,
    }
    plan["digest"] = plan_digest(plan)
    return plan


# Private files ----------------------------------------------------------------


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _write_private_json(path: Path, data: Mapping[str, Any]) -> None:
    """Atomically write a 0600 JSON file; the caller owns the parent directory."""

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise


@contextlib.contextmanager
def _exclusive_lock(state_dir: Path) -> Iterator[None]:
    _private_dir(state_dir)
    fd = os.open(state_dir / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StackUpdateBusy("another stack apply or rollback is running") from exc
        yield
    finally:
        os.close(fd)


def _new_record_id(env: Environment) -> str:
    return f"{env.now().strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def _record_path(manifest: StackManifest, record_id: str) -> Path:
    _require(bool(_RECORD_ID.match(record_id)), "invalid switch record id")
    records = manifest.state_dir / "switches"
    _private_dir(records)
    return records / f"{record_id}.json"


def read_record(manifest: StackManifest, record_id: str) -> dict[str, Any]:
    path = _record_path(manifest, record_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StackUpdateError("switch record not found") from exc
    _require(isinstance(data, dict), "switch record is malformed")
    return data


# Staging --------------------------------------------------------------------


def _run(
    env: Environment,
    argv: Sequence[str],
    *,
    timeout: float,
    extra_env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    environment = None if extra_env is None else {**os.environ, **extra_env}
    try:
        return env.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _stage_npm(
    manifest: StackManifest,
    component: Component,
    version: str,
    staging: Path,
    published: Mapping[str, str],
    env: Environment,
) -> dict[str, str]:
    npm = manifest.npm
    _require(npm is not None, "manifest.npm is required for npm sources")
    package = str(component.source.package)
    argv = [
        str(npm),
        "install",
        "--prefix",
        str(staging),
        "--no-save",
        "--no-audit",
        "--no-fund",
        "--no-update-notifier",
    ]
    if not component.source.lifecycle_scripts:
        argv.append("--ignore-scripts")
    argv.append(f"{package}@{version}")
    path = f"{Path(str(npm)).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    completed = _run(env, argv, timeout=_STAGE_TIMEOUT_SECONDS, extra_env={"PATH": path})
    _require(
        completed is not None and completed.returncode == 0,
        f"component {component.component_id}: npm install failed",
    )
    lock = staging / "package-lock.json"
    try:
        lock_data = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StackUpdateError(f"component {component.component_id}: no package lock") from exc
    entry = lock_data.get("packages", {}).get(f"node_modules/{package}", {})
    integrity = entry.get("integrity") if isinstance(entry, dict) else None
    _require(
        integrity == published.get("integrity"),
        f"component {component.component_id}: installed package does not match the plan",
    )
    return {"integrity": str(integrity)}


def _safe_zip_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        _require(target == root or root in target.parents, "archive member escapes its directory")
        mode = (member.external_attr >> 16) & 0o170000
        _require(mode != 0o120000, "archive contains a symbolic link")
    archive.extractall(destination)  # noqa: S202 - every member was checked above


def _stage_archive(
    component: Component,
    version: str,
    staging: Path,
    published: Mapping[str, str],
    env: Environment,
) -> dict[str, str]:
    url = str(component.source.url).format(version=version)
    data = env.get(url, _ARCHIVE_LIMIT)
    digest = hashlib.sha256(data).hexdigest()
    expected = published.get("sha256")
    _require(
        expected is None or digest == expected,
        f"component {component.component_id}: download does not match the published checksum",
    )
    name = _archive_name(url)
    download = staging.parent / f".{staging.name}.download"
    download.write_bytes(data)
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(download) as archive:
                _safe_zip_extract(archive, staging)
        else:
            try:
                with tarfile.open(download) as archive:
                    archive.extractall(staging, filter="data")
            except (tarfile.TarError, OSError) as exc:
                raise StackUpdateError(
                    f"component {component.component_id}: archive could not be extracted"
                ) from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            download.unlink()
    return {"sha256": digest}


def _stage(
    manifest: StackManifest,
    component: Component,
    step: Mapping[str, Any],
    env: Environment,
) -> Path:
    """Install one candidate into its immutable version directory."""

    version = str(step["to"])
    published = step.get("published") or {}
    final = component.versions_dir / version
    marker = final / MARKER_NAME
    if final.exists():
        try:
            existing = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StackUpdateError(
                f"component {component.component_id}: {version} exists but was not staged here"
            ) from exc
        _require(
            existing.get("component") == component.component_id
            and existing.get("version") == version
            and all(existing.get("artifact", {}).get(k) == v for k, v in published.items()),
            f"component {component.component_id}: staged {version} does not match the plan",
        )
        return final
    component.versions_dir.mkdir(parents=True, exist_ok=True)
    staging = component.versions_dir / f".{version}.staging-{secrets.token_hex(4)}"
    staging.mkdir(mode=0o755)
    try:
        if component.source.kind == "npm":
            artifact = _stage_npm(manifest, component, version, staging, published, env)
        else:
            artifact = _stage_archive(component, version, staging, published, env)
        (staging / MARKER_NAME).write_text(
            json.dumps(
                {"component": component.component_id, "version": version, "artifact": artifact},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        os.rename(staging, final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final


# Checks ---------------------------------------------------------------------


def _expand(argv: Sequence[str], directory: Path) -> list[str]:
    return [item.replace("{dir}", str(directory)) for item in argv]


def _argv_passes(env: Environment, argv: Sequence[str], expect: str | None, timeout: float) -> bool:
    completed = _run(env, argv, timeout=timeout)
    if completed is None or completed.returncode != 0:
        return False
    return expect is None or expect in (completed.stdout or "")[:_OUTPUT_LIMIT]


def _url_passes(env: Environment, url: str, expect: str | None) -> bool:
    try:
        body = env.get(url, _OUTPUT_LIMIT)
    except (OSError, StackUpdateError, ValueError):
        return False
    return expect is None or expect in body.decode("utf-8", "replace")


def _check_passes(env: Environment, check: Check, directory: Path) -> bool:
    if check.url is not None:
        return _url_passes(env, check.url, check.expect)
    return _argv_passes(env, _expand(check.argv, directory), check.expect, check.timeout_seconds)


def _eventually(env: Environment, timeout: float, probe: Callable[[], bool]) -> bool:
    deadline = env.monotonic() + timeout
    while True:
        if probe():
            return True
        if env.monotonic() >= deadline:
            return False
        env.sleep(1.0)


def _staged_checks(
    env: Environment, component: Component, directory: Path, version: str, allow_inference: bool
) -> str | None:
    """Return the first failing staged gate, or None."""

    if not _argv_passes(env, _expand(component.version_argv, directory), version, 30.0):
        return "version"
    for index, check in enumerate(component.checks):
        if check.phase != "staged" or (check.inference and not allow_inference):
            continue
        if not _check_passes(env, check, directory):
            return f"staged check {index}"
    return None


def _restart_units(env: Environment, units: Sequence[str]) -> bool:
    ok = True
    for unit in units:
        completed = _run(
            env, ("systemctl", "--user", "restart", unit), timeout=_UNIT_TIMEOUT_SECONDS
        )
        ok = ok and completed is not None and completed.returncode == 0
    return ok


def _unit_active(env: Environment, unit: str) -> bool:
    completed = _run(env, ("systemctl", "--user", "is-active", "--quiet", unit), timeout=10.0)
    return completed is not None and completed.returncode == 0


def _live_gates(env: Environment, component: Component, allow_inference: bool) -> str | None:
    """Return the first failing live gate after a restart, or None."""

    for unit in component.units:
        if not _eventually(env, 30.0, lambda unit=unit: _unit_active(env, unit)):
            return f"unit {unit}"
    for index, check in enumerate(component.checks):
        if check.phase != "live" or (check.inference and not allow_inference):
            continue
        if not _eventually(
            env,
            check.timeout_seconds,
            lambda check=check: _check_passes(env, check, component.link),
        ):
            return f"live check {index}"
    return None


# Apply and rollback -----------------------------------------------------------


def _pin_versions(manifest: StackManifest, versions: Mapping[str, str | None]) -> None:
    """Record the running versions as the manifest's pins (the drift baseline)."""

    data = json.loads(manifest.path.read_bytes())
    for item in data["components"]:
        version = versions.get(item["id"])
        if version is not None:
            item["version"] = version
    _write_private_json(manifest.path, data)


def _restore(
    env: Environment,
    manifest: StackManifest,
    flipped: Sequence[tuple[Component, str | None]],
) -> bool:
    ok = True
    for component, before in reversed(flipped):
        try:
            _flip_link(component.link, before)
        except OSError:
            ok = False
            continue
        ok = _restart_units(env, component.units) and ok
    return ok


def apply_plan(
    manifest: StackManifest,
    plan: Mapping[str, Any],
    *,
    expected_digest: str,
    env: Environment,
    allow_inference: bool = False,
) -> dict[str, Any]:
    """Stage, check and switch one exact plan; restore every link on a failed gate."""

    _require(plan.get("schema_version") == PLAN_SCHEMA_VERSION, "unsupported plan version")
    digest = plan_digest(plan)
    _require(plan.get("digest") == digest, "plan digest does not match its content")
    _require(expected_digest == digest, "plan digest does not match the approved digest")
    with _exclusive_lock(manifest.state_dir):
        _require(
            plan.get("manifest_digest") == manifest.digest,
            "the manifest changed since the plan was made",
        )
        current = {c.component_id: link_state(c) for c in manifest.components}
        _require(plan.get("links") == current, "stack links changed since the plan was made")
        steps = [(manifest.component(str(step["component"])), step) for step in plan["steps"]]
        staged: list[tuple[Component, Mapping[str, Any], Path]] = []
        for component, step in steps:
            directory = _stage(manifest, component, step, env)
            failure = _staged_checks(env, component, directory, str(step["to"]), allow_inference)
            _require(
                failure is None,
                f"component {component.component_id}: {failure} failed; no link was changed",
            )
            staged.append((component, step, directory))
        record_id = _new_record_id(env)
        record: dict[str, Any] = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "id": record_id,
            "kind": "switch",
            "plan_digest": digest,
            "started_at": env.now().isoformat(),
            "status": "started",
            "before": {c.component_id: current[c.component_id] for c, _, _ in staged},
            "after": {c.component_id: str(d) for c, _, d in staged},
            "flipped": [],
        }
        path = _record_path(manifest, record_id)
        _write_private_json(path, record)
        flipped: list[tuple[Component, str | None]] = []
        failure = None
        for component, _step, directory in staged:
            try:
                _flip_link(component.link, str(directory))
            except OSError:
                failure = f"component {component.component_id}: link switch failed"
                break
            flipped.append((component, current[component.component_id]))
            record["flipped"].append(component.component_id)
            _write_private_json(path, record)
            if not _restart_units(env, component.units):
                failure = f"component {component.component_id}: unit restart failed"
                break
            gate = _live_gates(env, component, allow_inference)
            if gate is not None:
                failure = f"component {component.component_id}: {gate} failed"
                break
        record["finished_at"] = env.now().isoformat()
        if failure is None:
            record["status"] = "completed"
            _pin_versions(manifest, {c.component_id: str(s["to"]) for c, s, _ in staged})
        else:
            record["failure"] = failure
            record["status"] = "restored" if _restore(env, manifest, flipped) else "restore_failed"
        _write_private_json(path, record)
        return record


def rollback_switch(manifest: StackManifest, record_id: str, *, env: Environment) -> dict[str, Any]:
    """Restore the links recorded before one switch, if nothing changed since."""

    with _exclusive_lock(manifest.state_dir):
        switch = read_record(manifest, record_id)
        _require(switch.get("kind") == "switch", "only a switch can be rolled back")
        _require(
            switch.get("status") in {"completed", "started", "restore_failed"},
            "this switch left no changed links to roll back",
        )
        flipped = [manifest.component(str(cid)) for cid in switch.get("flipped", [])]
        for component in flipped:
            _require(
                link_state(component) == switch["after"][component.component_id],
                f"component {component.component_id}: its link changed after this switch",
            )
        rollback_id = _new_record_id(env)
        record: dict[str, Any] = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "id": rollback_id,
            "kind": "rollback",
            "switch": record_id,
            "started_at": env.now().isoformat(),
        }
        ok = _restore(env, manifest, [(c, switch["before"][c.component_id]) for c in flipped])
        ok = ok and all(_unit_active(env, unit) for c in flipped for unit in c.units)
        record["status"] = "completed" if ok else "failed"
        record["finished_at"] = env.now().isoformat()
        if ok:
            _pin_versions(manifest, {c.component_id: installed_version(c) for c in flipped})
        _write_private_json(_record_path(manifest, rollback_id), record)
        return record


def stack_status(manifest: StackManifest) -> list[dict[str, Any]]:
    """Report each component's pinned and installed version without side effects."""

    return [
        {
            "component": component.component_id,
            "pinned": component.version,
            "installed": installed_version(component),
            "managed": link_state(component) is None or installed_version(component) is not None,
        }
        for component in manifest.components
    ]


# Pinned copy ------------------------------------------------------------------


def install_copy(destination: Path) -> Path:
    """Install this file as an immutable pinned copy and point ``current`` at it."""

    source = Path(__file__).resolve()
    data = source.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    directory = destination / digest
    target = directory / "stack_update.py"
    if not target.exists():
        _private_dir(directory)
        fd, temporary = tempfile.mkstemp(prefix=".stack_update.", dir=directory)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temporary, 0o500)
        os.replace(temporary, target)
    _require(hashlib.sha256(target.read_bytes()).hexdigest() == digest, "pinned copy differs")
    _flip_link(destination / "current", digest)
    return target


# CLI ------------------------------------------------------------------------


def _default_manifest() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "agents-projects-hub" / "stack-manifest.json"


def _default_copy_destination() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "agents-projects-hub" / "stack-update"


def _targets(
    manifest: StackManifest, pairs: Sequence[str], latest: Sequence[str], env: Environment
) -> dict[str, str]:
    targets: dict[str, str] = {}
    for pair in pairs:
        component_id, separator, version = pair.partition("=")
        _require(bool(separator), f"--set expects ID=VERSION, got {pair!r}")
        targets[component_id] = version
    for component_id in latest:
        targets[component_id] = resolve_latest(manifest.component(component_id), env)
    return targets


def main(argv: Sequence[str] | None = None, *, env: Environment | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="stack-update", description="Deterministic update tool for the agent stack."
    )
    parser.add_argument("--manifest", type=Path, default=_default_manifest())
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan", help="read-only plan for target versions")
    plan_parser.add_argument("--set", action="append", default=[], metavar="ID=VERSION")
    plan_parser.add_argument("--latest", action="append", default=[], metavar="ID")
    plan_parser.add_argument("--out", type=Path, required=True)
    apply_parser = commands.add_parser("apply", help="stage, check and switch one plan")
    apply_parser.add_argument("plan", type=Path)
    apply_parser.add_argument("--digest", required=True)
    apply_parser.add_argument("--allow-live-inference", action="store_true")
    rollback_parser = commands.add_parser("rollback", help="undo one recorded switch")
    rollback_parser.add_argument("switch")
    commands.add_parser("status", help="pinned and installed versions")
    copy_parser = commands.add_parser("install-copy", help="install a pinned copy of this tool")
    copy_parser.add_argument("--destination", type=Path, default=_default_copy_destination())
    args = parser.parse_args(argv)
    env = env or Environment()
    try:
        if args.command == "install-copy":
            result: Any = {"path": str(install_copy(args.destination))}
        else:
            manifest = load_manifest(args.manifest)
            if args.command == "plan":
                result = build_plan(manifest, _targets(manifest, args.set, args.latest, env), env)
                _write_private_json(args.out, result)
            elif args.command == "apply":
                plan = json.loads(args.plan.read_text(encoding="utf-8"))
                result = apply_plan(
                    manifest,
                    plan,
                    expected_digest=args.digest,
                    env=env,
                    allow_inference=args.allow_live_inference,
                )
            elif args.command == "rollback":
                result = rollback_switch(manifest, args.switch, env=env)
            else:
                result = stack_status(manifest)
    except StackUpdateError as exc:
        json.dump({"ok": False, "error": str(exc)}, sys.stdout)
        sys.stdout.write("\n")
        return 2
    json.dump({"ok": True, "result": result}, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if isinstance(result, dict) and result.get("status") in {
        "restored",
        "restore_failed",
        "failed",
    }:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
