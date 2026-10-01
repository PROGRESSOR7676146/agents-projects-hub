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
import http.client
import json
import os
import re
import secrets
import shutil
import stat
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
        link != versions_dir
        and versions_dir not in link.parents
        and link not in versions_dir.parents,
        f"{where}.link and versions_dir must not contain each other",
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

    return parse_manifest(_read_manifest_bytes(path), path=path)


def _read_manifest_bytes(path: Path) -> bytes:
    """Read the manifest through one no-follow descriptor after checking it."""

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise StackUpdateError("manifest not found") from exc
    except OSError as exc:
        raise StackUpdateError("manifest must be a regular file, not a symbolic link") from exc
    # Check and read through one descriptor, so the file cannot change in between.
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        _require(stat.S_ISREG(info.st_mode), "manifest must be a regular file")
        _require(info.st_uid == os.getuid(), "manifest must be owned by the current user")
        _require(info.st_mode & 0o022 == 0, "manifest must not be group- or world-writable")
        return handle.read()


# Link state -----------------------------------------------------------------


def link_state(component: Component) -> str | None:
    """Return the raw link target, None when absent, or a marker for a non-link."""

    return _link_state_at(component.link)


def _link_state_at(link: Path) -> str | None:
    """Read a link; only a missing path is None, any other failure is an error."""

    try:
        info = os.lstat(link)
        if stat.S_ISLNK(info.st_mode):
            return os.readlink(link)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StackUpdateError(f"link {link.name} cannot be read") from exc
    return NOT_A_LINK


def installed_version(component: Component) -> str | None:
    """Return the version directory the link selects, or None when unmanaged."""

    target = link_state(component)
    if target is None or target == NOT_A_LINK:
        return None
    path = Path(os.path.normpath(component.link.parent / target))
    if path.parent != component.versions_dir or not _VERSION.match(path.name):
        return None
    return path.name


def _flip_link(link: Path, target: str | None) -> None:
    """Atomically point ``link`` at ``target`` or remove it when ``target`` is None."""

    if target is None:
        with contextlib.suppress(FileNotFoundError):
            if os.path.islink(link):
                link.unlink()
                _fsync_dir(link.parent)
        return
    temporary = link.parent / f".{link.name}.stack-update-{secrets.token_hex(4)}"
    os.symlink(target, temporary)
    try:
        os.replace(temporary, link)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise
    _fsync_dir(link.parent)


def _fsync_dir(path: Path) -> None:
    """Persist a directory entry change (rename, link) across a host crash."""

    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# Network --------------------------------------------------------------------


class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    """Check every redirect before it is followed, not only the final URL."""

    def __init__(self, origin: str) -> None:
        self._https = origin.startswith("https://")
        self._loopback = urllib.parse.urlsplit(origin).hostname in _LOOPBACK_HOSTS

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        target = urllib.parse.urlsplit(newurl)
        if self._https and target.scheme != "https":
            raise StackUpdateError("an https request was redirected to plain http")
        if self._loopback and target.hostname not in _LOOPBACK_HOSTS:
            raise StackUpdateError("a loopback check was redirected off the machine")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _http_get(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "stack-update"})
    opener = urllib.request.build_opener(_GuardedRedirect(url))
    try:
        with opener.open(request, timeout=60) as response:
            final_url = str(response.geturl())
            data = response.read(limit + 1)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        host = urllib.parse.urlsplit(url).hostname
        raise StackUpdateError(f"request to {host} failed ({type(exc).__name__})") from exc
    if url.startswith("https://") and not final_url.startswith("https://"):
        raise StackUpdateError("an https request was redirected to plain http")
    if (
        urllib.parse.urlsplit(url).hostname in _LOOPBACK_HOSTS
        and urllib.parse.urlsplit(final_url).hostname not in _LOOPBACK_HOSTS
    ):
        raise StackUpdateError("a loopback check was redirected off the machine")
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
    url = str(source.url).format(version=version)
    if source.checksums_url is None:
        # Nothing is published: bind the plan to the bytes downloaded now, so
        # apply installs exactly what the owner approved.
        return {"sha256": hashlib.sha256(env.get(url, _ARCHIVE_LIMIT)).hexdigest()}
    checksums = env.get(source.checksums_url.format(version=version), _JSON_LIMIT)
    name = _archive_name(url)
    for line in checksums.decode("utf-8", "replace").splitlines():
        digest = _checksum_for(line, name)
        if digest is not None:
            return {"sha256": digest}
    raise StackUpdateError(f"component {component.component_id}: no published checksum for {name}")


_BSD_CHECKSUM = re.compile(r"^SHA256 \((?P<name>.+)\) = (?P<digest>[0-9A-Fa-f]{64})$")


def _checksum_for(line: str, name: str) -> str | None:
    """Parse one GNU (`digest  [*]path`) or BSD (`SHA256 (path) = digest`) line."""

    text = line.strip()
    bsd = _BSD_CHECKSUM.match(text)
    if bsd is not None:
        digest, path = bsd.group("digest"), bsd.group("name")
    else:
        parts = text.split(maxsplit=1)
        if len(parts) != 2:
            return None
        digest, path = parts[0], parts[1].strip().lstrip("*")
    if PurePosixPath(path).name != name or not _SHA256.match(digest.lower()):
        return None
    return digest.lower()


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
    unknown = sorted(set(targets) - {c.component_id for c in manifest.components})
    _require(not unknown, f"unknown components: {', '.join(unknown)}")
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
        step: dict[str, Any] = {
            "component": component.component_id,
            "from": current,
            "to": version,
            "source": _source_summary(component, version),
            "published": _published_digest(component, version, env),
        }
        if component.source.kind == "npm":
            lock = _npm_lock(manifest, component, version, env)
            root = _lock_packages(lock["package-lock.json"]).get(
                f"node_modules/{component.source.package}"
            )
            _require(
                isinstance(root, dict) and root.get("integrity") == step["published"]["integrity"],
                f"component {component.component_id}: npm lockfile disagrees with the registry",
            )
            step["lock"] = lock
            step["published"]["lock_sha256"] = hashlib.sha256(_canonical(lock)).hexdigest()
        steps.append(step)
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


def _private_dir(path: Path, *, root: Path) -> None:
    _durable_mkdir(path, mode=0o700, root=root)
    path.chmod(0o700)


def _durable_mkdir(path: Path, *, mode: int, root: Path) -> None:
    """Ensure ``root`` .. ``path`` exist and every entry is persisted in its parent.

    The parent entries are synced on every call, not only on creation, so a
    retry after a failed sync completes it instead of trusting existence.
    """

    _require(path == root or root in path.parents, "directory outside its root")
    # path and its ancestors down to and including root, deepest first
    chain = [path, *[parent for parent in path.parents if parent == root or root in parent.parents]]
    for directory in reversed(chain):
        if directory == root:
            directory.mkdir(mode=mode, parents=True, exist_ok=True)
        else:
            with contextlib.suppress(FileExistsError):
                directory.mkdir(mode=mode)
        _fsync_dir(directory.parent)


def _fsync_tree(root: Path) -> None:
    """Persist every file and directory of a staged tree before it is published."""

    def fail(error: OSError) -> None:
        raise error

    for directory, _subdirectories, files in os.walk(root, onerror=fail, followlinks=False):
        for name in files:
            entry = Path(directory) / name
            if entry.is_symlink() or not entry.is_file():
                continue
            fd = os.open(entry, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        _fsync_dir(Path(directory))


def _write_private_json(path: Path, data: Mapping[str, Any]) -> None:
    """Atomically write a 0600 JSON file; the caller owns the parent directory."""

    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
        raise
    _fsync_dir(path.parent)


@contextlib.contextmanager
def _exclusive_lock(state_dir: Path) -> Iterator[None]:
    _private_dir(state_dir, root=state_dir)
    fd = os.open(state_dir / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StackUpdateBusy("another stack apply or rollback is running") from exc
        yield
    finally:
        os.close(fd)


def _next_sequence(manifest: StackManifest) -> int:
    """A counter under the stack lock orders switches; wall clocks can go back."""

    path = manifest.state_dir / "sequence.json"
    try:
        current = int(json.loads(path.read_text(encoding="utf-8"))["sequence"])
    except FileNotFoundError:
        current = 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise StackUpdateError("the switch sequence cannot be read") from exc
    _write_private_json(path, {"sequence": current + 1})
    return current + 1


def _new_record_id(env: Environment) -> str:
    return f"{env.now().strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def _record_path(manifest: StackManifest, record_id: str) -> Path:
    _require(bool(_RECORD_ID.match(record_id)), "invalid switch record id")
    records = manifest.state_dir / "switches"
    _private_dir(records, root=manifest.state_dir)
    return records / f"{record_id}.json"


def read_record(manifest: StackManifest, record_id: str) -> dict[str, Any]:
    path = _record_path(manifest, record_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise StackUpdateError("switch record not found") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StackUpdateError("switch record is malformed") from exc
    _require(isinstance(data, dict), "switch record is malformed")
    return data


def _switch_records(manifest: StackManifest) -> list[dict[str, Any]]:
    records = manifest.state_dir / "switches"
    if not records.is_dir():
        return []
    found: list[dict[str, Any]] = []
    for path in sorted(records.glob("*.json")):
        record = read_record(manifest, path.stem)
        if record.get("kind") == "switch":
            found.append(record)
    return found


def _rollback_status(record: Mapping[str, Any]) -> str | None:
    rollback = record.get("rollback")
    return str(rollback.get("status")) if isinstance(rollback, dict) else None


def _require_reconciled(manifest: StackManifest) -> None:
    """Refuse a new switch while an earlier switch or rollback is unfinished."""

    for record in _switch_records(manifest):
        if record.get("status") == "started" and not record.get("flipped"):
            # An apply that stopped before its first link intent changed nothing.
            continue
        rollback = _rollback_status(record)
        _require(
            rollback != "started",
            f"the rollback of switch {record.get('id')} did not finish; repeat it first",
        )
        _require(
            record.get("status") not in {"started", "restore_failed"} or rollback == "completed",
            f"switch {record.get('id')} left links unreconciled; roll it back first",
        )


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


def _npm(
    manifest: StackManifest,
    env: Environment,
    arguments: Sequence[str],
    directory: Path,
    what: str,
) -> None:
    """Run npm on ``directory``; npm's own node must come first on PATH."""

    npm = manifest.npm
    _require(npm is not None, "manifest.npm is required for npm sources")
    argv = [
        str(npm),
        *arguments,
        "--prefix",
        str(directory),
        "--no-audit",
        "--no-fund",
        "--no-update-notifier",
    ]
    path = f"{Path(str(npm)).parent}{os.pathsep}{os.environ.get('PATH', '')}"
    completed = _run(env, argv, timeout=_STAGE_TIMEOUT_SECONDS, extra_env={"PATH": path})
    _require(completed is not None and completed.returncode == 0, f"{what} failed")


def _npm_major(manifest: StackManifest, env: Environment) -> int:
    completed = _run(env, [str(manifest.npm), "--version"], timeout=30.0)
    version = (completed.stdout or "").strip() if completed is not None else ""
    major = version.split(".", 1)[0]
    _require(completed is not None and major.isdigit(), "npm version cannot be determined")
    return int(major)


def _npm_lock(
    manifest: StackManifest, component: Component, version: str, env: Environment
) -> dict[str, Any]:
    """Resolve the complete dependency tree without downloading or running packages.

    The plan carries the resulting lockfile, so apply installs exactly the
    approved tree (versions and integrity of every package), not whatever a
    range resolves to later.
    """

    package = str(component.source.package)
    with tempfile.TemporaryDirectory(prefix="stack-update-plan-") as directory:
        root = Path(directory)
        # npm names the lockfile after the project; a fixed name keeps two plans
        # of the same tree byte-identical instead of naming the temporary dir.
        (root / "package.json").write_text(
            json.dumps({"name": f"stack-update-{component.component_id}", "private": True}) + "\n",
            encoding="utf-8",
        )
        _npm(
            manifest,
            env,
            ["install", "--package-lock-only", "--save-exact", "--ignore-scripts"]
            + [f"{package}@{version}"],
            root,
            f"component {component.component_id}: npm dependency resolution",
        )
        try:
            package_json = json.loads((root / "package.json").read_text(encoding="utf-8"))
            lockfile = json.loads((root / "package-lock.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StackUpdateError(
                f"component {component.component_id}: npm produced no lockfile"
            ) from exc
    return {"package.json": package_json, "package-lock.json": lockfile}


def _lock_packages(lockfile: object) -> dict[str, Any]:
    packages = lockfile.get("packages") if isinstance(lockfile, dict) else None
    return cast(dict[str, Any], packages) if isinstance(packages, dict) else {}


def _stage_npm(
    manifest: StackManifest,
    component: Component,
    staging: Path,
    step: Mapping[str, Any],
    env: Environment,
) -> dict[str, str]:
    """Install exactly the planned tree; run package scripts only after verifying it."""

    lock = step["lock"]
    published = step["published"]
    for name in ("package.json", "package-lock.json"):
        (staging / name).write_text(json.dumps(lock[name], indent=2) + "\n", encoding="utf-8")
    what = f"component {component.component_id}: npm ci"
    _npm(manifest, env, ["ci", "--ignore-scripts"], staging, what)
    # npm ci verified every tarball against the lockfile; confirm what it
    # recorded as installed matches the plan before any package code runs.
    try:
        installed = json.loads(
            (staging / "node_modules" / ".package-lock.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StackUpdateError(f"component {component.component_id}: no npm lockfile") from exc
    planned = _lock_packages(lock["package-lock.json"])
    root_entry = _lock_packages(installed).get(f"node_modules/{component.source.package}")
    _require(
        isinstance(root_entry, dict)
        and root_entry.get("integrity") == published.get("integrity")
        and all(
            isinstance(entry, dict)
            and isinstance(planned.get(name), dict)
            and entry.get("integrity") == planned[name].get("integrity")
            for name, entry in _lock_packages(installed).items()
        ),
        f"component {component.component_id}: installed packages do not match the plan",
    )
    if component.source.lifecycle_scripts:
        # npm 12 skips install scripts not covered by allowScripts and still
        # exits 0, which would mark an unbuilt version as staged.
        _require(
            _npm_major(manifest, env) < 12,
            f"component {component.component_id}: lifecycle scripts need npm older than 12",
        )
        what = f"component {component.component_id}: npm lifecycle scripts"
        _npm(manifest, env, ["rebuild"], staging, what)
    return {
        "integrity": str(published["integrity"]),
        "lock_sha256": str(published["lock_sha256"]),
    }


def _safe_zip_extract(archive: zipfile.ZipFile, destination: Path) -> None:
    root = destination.resolve()
    for member in archive.infolist():
        target = (destination / member.filename).resolve()
        _require(target == root or root in target.parents, "archive member escapes its directory")
        mode = (member.external_attr >> 16) & 0o170000
        _require(mode != 0o120000, "archive contains a symbolic link")
    archive.extractall(destination)  # noqa: S202 - every member was checked above
    # zipfile drops POSIX modes; restore the executable bit, never setuid/setgid.
    for member in archive.infolist():
        if not member.is_dir() and (member.external_attr >> 16) & 0o111:
            (destination / member.filename).chmod(0o755)


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
    _require(
        digest == published.get("sha256"),
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
        _require(
            component.source.kind != "npm"
            or existing.get("lifecycle_scripts", False) == component.source.lifecycle_scripts,
            f"component {component.component_id}: staged {version} was prepared with another "
            "lifecycle-scripts setting; remove that version directory to stage it again",
        )
        # A retry after a failed sync must finish it; existence proves nothing.
        _fsync_tree(final)
        _fsync_dir(component.versions_dir)
        return final
    _durable_mkdir(component.versions_dir, mode=0o755, root=component.versions_dir)
    staging = component.versions_dir / f".{version}.staging-{secrets.token_hex(4)}"
    staging.mkdir(mode=0o755)
    try:
        if component.source.kind == "npm":
            artifact = _stage_npm(manifest, component, staging, step, env)
        else:
            artifact = _stage_archive(component, version, staging, published, env)
        (staging / MARKER_NAME).write_text(
            json.dumps(
                {
                    "component": component.component_id,
                    "version": version,
                    "artifact": artifact,
                    "lifecycle_scripts": component.source.lifecycle_scripts,
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        # The staged tree must be on disk before its name can become a link target.
        _fsync_tree(staging)
        os.rename(staging, final)
        _fsync_dir(component.versions_dir)
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
    output = (completed.stdout or "")[:_OUTPUT_LIMIT] + (completed.stderr or "")[:_OUTPUT_LIMIT]
    return expect is None or expect in output


def _url_passes(env: Environment, url: str, expect: str | None) -> bool:
    try:
        body = env.get(url, _JSON_LIMIT)
    except (OSError, ValueError, StackUpdateError, http.client.HTTPException):
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


@dataclass(frozen=True, slots=True)
class _Flip:
    """One link a switch changes, recorded with everything needed to undo it."""

    component_id: str
    link: Path
    before: str | None
    after: str
    units: tuple[str, ...]
    pin_before: str

    def record(self) -> dict[str, Any]:
        return {
            "link": str(self.link),
            "before": self.before,
            "after": self.after,
            "units": list(self.units),
            "pin_before": self.pin_before,
        }

    @classmethod
    def from_record(cls, component_id: str, raw: object) -> _Flip:
        _require(isinstance(raw, dict), "switch record is malformed")
        data = cast(dict[str, Any], raw)
        link, before, after = data.get("link"), data.get("before"), data.get("after")
        units, pin = data.get("units"), data.get("pin_before")
        _require(
            isinstance(link, str)
            and link.startswith("/")
            and (before is None or isinstance(before, str))
            and isinstance(after, str)
            and isinstance(units, list)
            and all(isinstance(unit, str) and _UNIT.match(unit) for unit in units)
            and isinstance(pin, str)
            and bool(_VERSION.match(pin)),
            "switch record is malformed",
        )
        return cls(
            component_id=component_id,
            link=Path(str(link)),
            before=cast(str | None, before),
            after=str(after),
            units=tuple(str(unit) for unit in cast(list[object], units)),
            pin_before=str(pin),
        )


def _restore(env: Environment, flips: Sequence[_Flip]) -> bool:
    """Point every link back at its "before" target and restart its units."""

    ok = True
    for flip in reversed(flips):
        try:
            _flip_link(flip.link, flip.before)
        except OSError:
            ok = False
            continue
        ok = _restart_units(env, flip.units) and ok
    return ok


def _restore_pins(manifest: StackManifest, flips: Sequence[_Flip]) -> bool:
    try:
        _pin_versions(manifest, {flip.component_id: flip.pin_before for flip in flips})
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True


def _require_unchanged(manifest: StackManifest, plan: Mapping[str, Any]) -> None:
    """The manifest file and every link must still be exactly what the plan saw."""

    # Same trust checks as the first load: no symlink, owner, no group/world write.
    raw = _read_manifest_bytes(manifest.path)
    _require(
        plan.get("manifest_digest") == manifest.digest == hashlib.sha256(raw).hexdigest(),
        "the manifest changed since the plan was made",
    )
    current = {c.component_id: link_state(c) for c in manifest.components}
    _require(plan.get("links") == current, "stack links changed since the plan was made")


def _valid_npm_step(step: Mapping[str, Any]) -> bool:
    published = step.get("published", {})
    lock = step.get("lock")
    return (
        str(published.get("integrity", "")).startswith("sha512-")
        and isinstance(lock, dict)
        and isinstance(lock.get("package.json"), dict)
        and isinstance(lock.get("package-lock.json"), dict)
        and published.get("lock_sha256") == hashlib.sha256(_canonical(lock)).hexdigest()
    )


def _plan_steps(
    manifest: StackManifest, plan: Mapping[str, Any]
) -> list[tuple[Component, Mapping[str, Any]]]:
    """Revalidate every step: a self-consistent digest does not make a plan well formed."""

    raw_steps = plan.get("steps")
    _require(isinstance(raw_steps, list) and bool(raw_steps), "plan has no steps")
    steps: list[tuple[Component, Mapping[str, Any]]] = []
    for raw in cast(list[object], raw_steps):
        _require(isinstance(raw, dict), "plan step is malformed")
        step = cast(dict[str, Any], raw)
        component = manifest.component(str(step.get("component")))
        version = step.get("to")
        _require(
            isinstance(version, str) and bool(_VERSION.match(version)),
            f"component {component.component_id}: plan has an invalid version",
        )
        published = step.get("published", {})
        _require(
            isinstance(published, dict)
            and all(isinstance(k, str) and isinstance(v, str) for k, v in published.items())
            and (
                _valid_npm_step(step)
                if component.source.kind == "npm"
                else bool(_SHA256.match(str(published.get("sha256", ""))))
            ),
            f"component {component.component_id}: plan has a malformed published digest",
        )
        steps.append((component, step))
    ids = [component.component_id for component, _ in steps]
    orders = [component.order for component, _ in steps]
    _require(len(set(ids)) == len(ids), "plan names a component twice")
    _require(orders == sorted(orders), "plan steps are not in dependency order")
    return steps


def _switch(
    env: Environment,
    staged: Sequence[tuple[Component, _Flip]],
    record: dict[str, Any],
    path: Path,
    flipped: list[_Flip],
    allow_inference: bool,
) -> str | None:
    """Switch staged components in order; return the first failure, or None."""

    for component, flip in staged:
        # Record the intent durably before the flip: after a crash, rollback
        # finds every link this switch may have changed.
        flipped.append(flip)
        record["flipped"].append(flip.component_id)
        _write_private_json(path, record)
        try:
            _flip_link(flip.link, flip.after)
        except OSError:
            # The rename may have happened before the error (for example in the
            # directory fsync); only a link confirmed at "before" is left out,
            # an unreadable one is restored like a switched one.
            try:
                unchanged = _link_state_at(flip.link) == flip.before
            except StackUpdateError:
                unchanged = False
            if unchanged:
                flipped.pop()
            return f"component {component.component_id}: link switch failed"
        if not _restart_units(env, flip.units):
            return f"component {component.component_id}: unit restart failed"
        gate = _live_gates(env, component, allow_inference)
        if gate is not None:
            return f"component {component.component_id}: {gate} failed"
    return None


def _undo(
    env: Environment,
    manifest: StackManifest,
    record: dict[str, Any],
    path: Path,
    flips: Sequence[_Flip],
    flipped: Sequence[_Flip],
    failure: str,
) -> None:
    """Put back every switched link and every pin, then record the outcome."""

    ok = _restore(env, flipped)
    ok = _restore_pins(manifest, flips) and ok
    record["failure"] = failure
    record["status"] = "restored" if ok else "restore_failed"
    record["finished_at"] = env.now().isoformat()
    try:
        _write_private_json(path, record)
    except OSError as exc:
        raise StackUpdateError(
            f"switch {record['status']}, but its record could not be written"
        ) from exc


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
        _require_unchanged(manifest, plan)
        _require_reconciled(manifest)
        steps = _plan_steps(manifest, plan)
        staged: list[tuple[Component, _Flip]] = []
        for component, step in steps:
            directory = _stage(manifest, component, step, env)
            failure = _staged_checks(env, component, directory, str(step["to"]), allow_inference)
            _require(
                failure is None,
                f"component {component.component_id}: {failure} failed; no link was changed",
            )
            flip = _Flip(
                component_id=component.component_id,
                link=component.link,
                before=plan["links"][component.component_id],
                after=str(directory),
                units=component.units,
                pin_before=component.version,
            )
            staged.append((component, flip))
        # Staging can take minutes: recheck right before the first link changes.
        _require_unchanged(manifest, plan)
        flips = [flip for _, flip in staged]
        record_id = _new_record_id(env)
        record: dict[str, Any] = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "id": record_id,
            "kind": "switch",
            "sequence": _next_sequence(manifest),
            "plan_digest": digest,
            "started_at": env.now().isoformat(),
            "status": "started",
            "components": {flip.component_id: flip.record() for flip in flips},
            "flipped": [],
        }
        path = _record_path(manifest, record_id)
        _write_private_json(path, record)
        flipped: list[_Flip] = []
        try:
            failure = _switch(env, staged, record, path, flipped, allow_inference)
            if failure is None:
                # "completed" means links, pins and record agree; any failure up
                # to and including the final write undoes links and pins.
                _pin_versions(manifest, {c.component_id: str(s["to"]) for c, s in steps})
                record["status"] = "completed"
                record["finished_at"] = env.now().isoformat()
                _write_private_json(path, record)
                return record
        except Exception as exc:
            failure = f"unexpected {type(exc).__name__} during the switch"
        except BaseException:
            # An interrupt still restores the stack before it propagates.
            _undo(env, manifest, record, path, flips, flipped, "interrupted during the switch")
            raise
        _undo(env, manifest, record, path, flips, flipped, failure)
        return record


def rollback_switch(manifest: StackManifest, record_id: str, *, env: Environment) -> dict[str, Any]:
    """Undo one switch from its own record; repeat it until it completes."""

    with _exclusive_lock(manifest.state_dir):
        switch = read_record(manifest, record_id)
        _require(switch.get("kind") == "switch", "only a switch can be rolled back")
        _require(switch.get("status") != "restored", "apply already restored this switch")
        _require(_rollback_status(switch) != "completed", "this switch was already rolled back")
        components = switch.get("components")
        flipped_ids = switch.get("flipped")
        _require(
            isinstance(components, dict) and isinstance(flipped_ids, list),
            "switch record is malformed",
        )
        recorded = cast(dict[str, Any], components)
        flips = [
            _Flip.from_record(str(component_id), recorded.get(str(component_id)))
            for component_id in cast(list[object], flipped_ids)
        ]
        _require(bool(flips), "this switch changed no link")
        # A later switch owns these links even when it set the same targets.
        mine = {flip.component_id for flip in flips}
        for later in _switch_records(manifest):
            if int(later.get("sequence", 0)) <= int(switch.get("sequence", 0)):
                continue
            touched = mine & {str(cid) for cid in later.get("flipped", [])}
            _require(
                not touched
                or later.get("status") == "restored"
                or _rollback_status(later) == "completed",
                f"a later switch {later.get('id')} changed {', '.join(sorted(touched))}; "
                "roll that back first",
            )
        for flip in flips:
            # Undo exactly the recorded link; a manifest that now names another
            # link for this component is refused before anything changes.
            _require(
                manifest.component(flip.component_id).link == flip.link,
                f"component {flip.component_id}: its link path changed since this switch",
            )
            _require(
                _link_state_at(flip.link) in (flip.before, flip.after),
                f"component {flip.component_id}: its link changed after this switch",
            )
        rollback_id = _new_record_id(env)
        # Mark the rollback on the switch itself before any change: a repeat can
        # complete it, a new apply waits for it, and once completed it is final.
        switch["rollback"] = {"id": rollback_id, "status": "started"}
        _write_private_json(_record_path(manifest, record_id), switch)
        record: dict[str, Any] = {
            "schema_version": RECORD_SCHEMA_VERSION,
            "id": rollback_id,
            "kind": "rollback",
            "switch": record_id,
            "started_at": env.now().isoformat(),
            "status": "started",
            "components": [flip.component_id for flip in flips],
        }
        path = _record_path(manifest, rollback_id)
        _write_private_json(path, record)
        # Every step is idempotent, so a rollback interrupted after some links,
        # restarts or pins completes when repeated.
        ok = _restore(env, flips)
        ok = all(_unit_active(env, unit) for flip in flips for unit in flip.units) and ok
        ok = _restore_pins(manifest, flips) and ok
        record["status"] = "completed" if ok else "failed"
        record["finished_at"] = env.now().isoformat()
        _write_private_json(path, record)
        if ok:
            switch["rollback"] = {"id": rollback_id, "status": "completed"}
            _write_private_json(_record_path(manifest, record_id), switch)
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
        _private_dir(directory, root=destination)
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


def _read_plan(path: Path) -> dict[str, Any]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StackUpdateError("plan file cannot be read") from exc
    _require(isinstance(plan, dict), "plan file is malformed")
    return cast(dict[str, Any], plan)


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
                plan = _read_plan(args.plan)
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
