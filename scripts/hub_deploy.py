#!/usr/bin/env python3
"""Standalone Hub release planning, deployment windows and recovery."""

from __future__ import annotations

import argparse
import copy
import decimal
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Callable

try:
    from scripts import hub_claude_store as claude_store
except ImportError:  # run as `python3.12 -I scripts/hub_deploy.py`: no script dir on sys.path
    import importlib.util

    _spec = importlib.util.spec_from_file_location(
        "hub_claude_store", Path(__file__).with_name("hub_claude_store.py")
    )
    assert _spec is not None and _spec.loader is not None
    claude_store = importlib.util.module_from_spec(_spec)
    sys.modules["hub_claude_store"] = claude_store
    _spec.loader.exec_module(claude_store)

_HEX = re.compile(r"[0-9a-f]{64}\Z")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_VERSION = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")
BASE_UNITS = (
    "agents-projects-hub.service",
    "agents-projects-hub@.service",
    "agents-projects-hub-worker@.service",
    "agents-projects-hub-codex-worker@.service",
    "agents-projects-hub-claude-worker@.service",
    "agents-projects-hub-sender.service",
    "agents-projects-hub-project-provisioner.service",
    "agents-projects-hub-monitor.service",
    "agents-projects-hub-codex-app-server.service",
    "agents-projects-hub-codex-socket-dir.service",
)


class Refused(ValueError):
    """A required deployment safety condition was not established."""


def need(condition: object, message: str) -> None:
    if not condition:
        raise Refused(message)


# Owner decision 2026-11-21 (ADR 0094): a same-schema plan rolls back to the
# release installed before the window and never names another one.
SAME_SCHEMA_ROLLBACK = (
    "a same-schema plan rolls back to the previous release; no --rollback-release"
)
DECLARATION = "explicit_artifact_loss_declaration"
WINDOW_DONE = ("ACCEPTED", "ROLLED_BACK", "CORE_HEALTHY_MONITOR_INCOMPLETE")
AMENDMENT_DONE = ("ACCEPTED", "REVERTED", "CORE_HEALTHY_MONITOR_INCOMPLETE")
AMENDMENT_EFFECTIVE = ("ACCEPTED", "CORE_HEALTHY_MONITOR_INCOMPLETE")
MANIFEST = "release-manifest.json"
# Top-level Hub config keys that `reconfigure` may change on an accepted
# window. Only the Controller, the sender and the monitor, which reads the Hub
# config on every run, read them. Keys that Claude workers read from their own
# copy, destinations such as the alert topic, trust, credentials, project
# registration, security profiles, paths, worker counts and the unit set stay
# out of reach.
RECONFIGURABLE_KEYS = frozenset(
    {
        # Workers read agent mail routes from the database, where the
        # sender projects them at start (ADR 0112).
        "agent_mail",
        "message_batch_max_ms",
        "message_batch_quiet_ms",
        # Workers read the handoff setting from the database, where the
        # sender projects it at start (ADR 0114).
        "session_handoff",
        "task_no_progress_seconds",
        # Only the doctor and the monitor read the tlive switch; the config
        # refuses it unless no Hub work can ask a human through tlive (ADR 0116).
        "tlive_enabled",
        "topic_panel",
    }
)


def canonical_document(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def bind_plan(plan: dict[str, Any]) -> dict[str, Any]:
    snapshot = copy.deepcopy(plan)
    return {"plan": snapshot, "sha256": hashlib.sha256(canonical_document(snapshot)).hexdigest()}


def verify_plan(bound: dict[str, Any]) -> dict[str, Any]:
    need(isinstance(bound, dict) and set(bound) == {"plan", "sha256"}, "invalid bound plan")
    plan = bound["plan"]
    need(isinstance(plan, dict), "invalid plan")
    need(
        isinstance(bound["sha256"], str)
        and hashlib.sha256(canonical_document(plan)).hexdigest() == bound["sha256"],
        "plan digest mismatch",
    )
    return copy.deepcopy(plan)


def check_schema(
    old: dict[str, Any], new: dict[str, Any], live: int, rollback: dict[str, Any] | None = None
) -> None:
    old_schema, new_schema = old.get("schema_max"), new.get("schema_max")
    if not (
        type(old_schema) is int
        and old_schema >= 0
        and type(new_schema) is int
        and new_schema >= 0
        and type(live) is int
        and live >= 0
    ):
        raise Refused("missing schema evidence")
    need(old_schema == live, "old release schema mismatch")
    need(new_schema >= live, "candidate schema downgrade refused")
    if new_schema == live:
        need(rollback is None, SAME_SCHEMA_ROLLBACK)
    elif rollback is None:
        raise Refused("forward migration requires --rollback-release")
    else:
        need(rollback.get("schema_max") == new_schema, "rollback release schema mismatch")
    if new_schema >= 59:
        need(
            new.get(DECLARATION) is True,
            "candidate schema-59 release must permit artifact-loss declarations",
        )
        if rollback is None:
            # The previous release keeps its own verified capability: rollback
            # restores exactly the authority in effect before the window.
            need(
                type(old.get(DECLARATION)) is bool,
                "previous release declaration capability is missing",
            )
        else:
            need(
                rollback.get(DECLARATION) is False,
                "rollback schema-59 release must refuse artifact-loss declarations",
            )


def _wheel_digest(info: dict[str, Any], wheel: Path) -> str:
    """The wheel digest that verify_assets checked against the release."""
    digests = [a["sha256"] for a in info["artifacts"] if a["name"] == wheel.name]
    need(len(digests) == 1, "missing verified wheel digest")
    return digests[0]


def _bound_digest(path: str, verified: str) -> str:
    need(
        hashlib.sha256(Path(path).read_bytes()).hexdigest() == verified, "release artifact changed"
    )
    return verified


def _live_schema(installation: dict[str, Any]) -> int:
    with closing(
        sqlite3.connect(Path(installation["state_db"]).as_uri() + "?mode=ro", uri=True)
    ) as db:
        return int(db.execute("PRAGMA user_version").fetchone()[0])


def discover_dropin_root(dropins: list[str]) -> str:
    """Resolve the common runtime from each managed ExecStart, refusing ambiguity."""
    need(bool(dropins), "no managed drop-ins")
    roots: set[str] = set()
    for text in dropins:
        commands = [
            line.partition("=")[2] for line in text.splitlines() if line.startswith("ExecStart=")
        ]
        commands = [command for command in commands if command.strip()]
        need(len(commands) == 1, "ambiguous ExecStart")
        try:
            argv = shlex.split(commands[0])
        except ValueError as exc:
            raise Refused("invalid ExecStart") from exc
        need(bool(argv), "empty ExecStart")
        executable = Path(argv[0])
        need(
            executable.is_absolute()
            and executable.parent.name == "bin"
            and executable.parent.parent.name == "venv"
            and ".." not in executable.parts,
            "unsupported ExecStart runtime",
        )
        roots.add(str(executable.parent.parent.parent))
    need(len(roots) == 1, "mixed runtime roots")
    return roots.pop()


def _host_permissions(permissions: dict[str, Any]) -> bool:
    """The explicit host profile (ADR 0095): profile, session storage and an optional route file."""
    if permissions.get("profile") != "host":
        return False
    need(
        {"profile", "provider_home"}
        <= set(permissions)
        <= {"profile", "provider_home", "route_environment"},
        "invalid Claude host permissions",
    )
    return True


# Release-specific Claude permission keys; every other setting is policy.
_RELEASE_PERMISSION_KEYS = frozenset(
    {"runtime_roots", "python_executable", "hook_code_root", "user_releases"}
)


def claude_policy(config: object) -> dict[str, Any]:
    """Claude settings that the Hub config and the Claude worker config must share.

    Only the release overlay may differ: the CLI executable and the runtime paths
    and pin of a confined profile. The monitor and Controller read the Hub config,
    so a drifted copy there misreports the Claude workers' real configuration.
    """
    need(isinstance(config, dict), "Hub config must be an object")
    assert isinstance(config, dict)
    agents = config.get("agents", [])
    need(isinstance(agents, list), "invalid agents")
    permissions = config.get("claude_file_permissions")
    claude = [
        {key: value for key, value in agent.items() if key != "executable"}
        for agent in agents
        if isinstance(agent, dict) and agent.get("runtime") == "claude"
    ]
    identities = [json.dumps(agent.get("agent_id")) for agent in claude]
    need(len(set(identities)) == len(identities), "duplicate Claude agent identity")
    return {
        # Compared by agent identity, never by list position.
        "agents": sorted(claude, key=lambda agent: json.dumps(agent.get("agent_id"))),
        "claude_file_permissions": {
            key: value for key, value in permissions.items() if key not in _RELEASE_PERMISSION_KEYS
        }
        if isinstance(permissions, dict)
        else None,
    }


def verify_config_pin(
    config: dict[str, Any], old: str, verify: Callable[[str, str], bool], *, user_owned: bool
) -> None:
    permissions = config.get("claude_file_permissions")
    if not isinstance(permissions, dict):
        raise Refused("missing Claude permissions")
    pins = permissions.get("user_releases", [])
    need(isinstance(pins, list) and len(pins) <= 1, "ambiguous runtime pins")
    need(bool(pins) or not user_owned, "user runtime has no pin")
    if pins:
        pin = pins[0]
        need(
            isinstance(pin, dict)
            and set(pin) == {"root", "sha256"}
            and pin["root"] == old
            and isinstance(pin["sha256"], str)
            and _HEX.fullmatch(pin["sha256"]) is not None,
            "invalid runtime pin",
        )
        need(verify(old, pin["sha256"]), "runtime pin verification failed")


def _path_rewrite(value: object, old: str, new: str) -> str:
    if not isinstance(value, str):
        raise Refused("expected path string")
    if value == old or value.startswith(old + "/"):
        return new + value[len(old) :]
    return value


def _root_text(value: object, old: str) -> bool:
    if isinstance(value, str):
        return old in value
    if isinstance(value, list):
        return any(_root_text(item, old) for item in value)
    if isinstance(value, dict):
        return any(_root_text(key, old) or _root_text(item, old) for key, item in value.items())
    return False


def rewrite_claude_config(
    config: dict[str, Any],
    old: str,
    new: str,
    tree_sha256: str,
    *,
    claude_executable: str | None = None,
) -> dict[str, Any]:
    need(isinstance(config, dict), "Claude config must be an object")
    need(old.startswith("/") and new.startswith("/") and old != new, "invalid runtime roots")
    need(_HEX.fullmatch(tree_sha256) is not None, "invalid runtime digest")
    result = copy.deepcopy(config)
    agents = result.get("agents")
    permissions = result.get("claude_file_permissions")
    if not isinstance(agents, list) or not isinstance(permissions, dict):
        raise Refused("unsupported Claude config")
    for agent in agents:
        need(isinstance(agent, dict), "invalid agent")
        if agent.get("runtime") == "claude" and "executable" in agent:
            agent["executable"] = (
                claude_executable
                if claude_executable is not None
                else _path_rewrite(agent["executable"], old, new)
            )
    if _host_permissions(permissions):
        return result  # host carries no runtime paths or pin beyond the CLI executable
    roots = permissions.get("runtime_roots")
    if not isinstance(roots, list):
        raise Refused("invalid runtime_roots")
    permissions["runtime_roots"] = [_path_rewrite(path, old, new) for path in roots]
    for key in ("python_executable", "hook_code_root"):
        need(key in permissions, "missing " + key)
        permissions[key] = _path_rewrite(permissions[key], old, new)
    pins = permissions.get("user_releases", [])
    need(isinstance(pins, list) and len(pins) <= 1, "ambiguous previous runtime pins")
    if pins:
        pin = pins[0]
        need(
            isinstance(pin, dict)
            and set(pin) == {"root", "sha256"}
            and pin["root"] == old
            and isinstance(pin["sha256"], str)
            and _HEX.fullmatch(pin["sha256"]) is not None,
            "unexpected previous runtime pin",
        )
    permissions["user_releases"] = [{"root": new, "sha256": tree_sha256}]
    need(not _root_text(result, old), "old runtime referenced by unsupported config field")
    return result


def rewrite_dropin(text: str, old: str, new: str, old_config: str, new_config: str) -> str:
    need(old.startswith("/") and new.startswith("/") and old != new, "invalid runtime roots")
    need(all(name and "/" not in name for name in (old_config, new_config)), "invalid config name")
    lines = []
    for line in text.splitlines(keepends=True):
        if line.startswith(("ExecStart=", "ExecStartPost=", "ExecStartPre=", "ExecStopPost=")):
            line = re.sub(
                r"^(ExecStart(?:Post|Pre)?|ExecStopPost)(=[-+!:@]*[\"']?)" + re.escape(old) + r"/",
                lambda match: match.group(1) + match.group(2) + new + "/",
                line,
                count=1,
            )
            line = re.sub(
                r"(?<![\w.-])" + re.escape(old_config) + r"(?=$|[\s\"'])",
                lambda _: new_config,
                line,
            )
        elif line.startswith("Environment="):
            directive, value = line.split("=", 1)
            try:
                shlex.split(value)
            except ValueError as exc:
                raise Refused("invalid service environment") from exc

            def rewrite_assignment(match: re.Match[str]) -> str:
                token = match.group()
                return re.sub(
                    r"^([\"']?[A-Za-z_][A-Za-z0-9_]*=[\"']?)" + re.escape(old) + r"(?=/|[\"']|$)",
                    lambda part: part.group(1) + new,
                    token,
                    count=1,
                )

            line = (
                directive
                + "="
                + re.sub(
                    r"""(?:"(?:\\.|[^"\\])*"|'[^']*'|\\.|[^\s"'\\])+""",
                    rewrite_assignment,
                    value,
                )
            )
        lines.append(line)
    result = "".join(lines)
    need(
        old not in result and (old_config == new_config or old_config not in result),
        "unsupported drop-in reference",
    )
    return result


def verify_assets(directory: Path, tag: str) -> tuple[dict[str, Any], Path]:
    need(_VERSION.fullmatch(tag) is not None, "release must be an exact version tag")
    lines = (directory / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    entries: dict[str, str] = {}
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.+-]+)", line)
        need(match is not None, "invalid SHA256SUMS")
        assert match is not None
        checksum, name = match.groups()
        need(name not in entries and name not in (".", ".."), "duplicate or invalid asset")
        path = directory / name
        need(path.is_file() and not path.is_symlink(), "missing or unsafe asset")
        need(hashlib.sha256(path.read_bytes()).hexdigest() == checksum, "asset digest mismatch")
        entries[name] = checksum
    prefix = f"agents_projects_hub-{tag[1:]}"
    wheel = f"{prefix}-py3-none-any.whl"
    expected = {
        wheel,
        f"{prefix}.tar.gz",
        "requirements-release.lock",
        "requirements-build.lock",
        "uv.lock",
        "release-info.json",
    }
    need(set(entries) == expected, "unexpected release asset set")
    info = json.loads((directory / "release-info.json").read_text(encoding="utf-8"))
    need(isinstance(info, dict), "invalid release metadata")
    need(
        info.get("package_version") == tag[1:] and info.get("clean_tree") is True,
        "release metadata mismatch",
    )
    sha = info.get("git_sha")
    need(isinstance(sha, str) and _SHA.fullmatch(sha) is not None, "invalid release revision")
    need(type(info.get("schema_max")) is int and info["schema_max"] >= 0, "missing release schema")
    if info["schema_max"] >= 59:
        need(
            type(info.get("explicit_artifact_loss_declaration")) is bool,
            "missing schema-59 declaration capability",
        )
    assets = info.get("artifacts")
    need(isinstance(assets, list) and len(assets) == len(expected) - 1, "invalid artifact list")
    seen = set()
    for asset in assets:
        need(
            isinstance(asset, dict) and set(asset) == {"name", "sha256", "size"}, "invalid artifact"
        )
        name = asset["name"]
        need(
            isinstance(name, str) and name in expected - {"release-info.json"} and name not in seen,
            "unexpected artifact",
        )
        seen.add(name)
        need(
            asset["sha256"] == entries[name]
            and type(asset["size"]) is int
            and asset["size"] == (directory / name).stat().st_size,
            "artifact metadata mismatch",
        )
    return info, directory / wheel


_PATH_FIELDS = (
    "units_dir",
    "hub_config",
    "claude_config_dir",
    "codex_manifest",
    "state_db",
    "releases_dir",
    "ops_dir",
    "native_claude",
)
_INPUT_FIELDS = set(_PATH_FIELDS) | {
    "schema_version",
    "claude_config_pattern",
    "managed_dropin_name",
    "native_sha256",
    "restart_order",
    "config_only_units",
    "controller_unit",
    "untouched_units",
    "monitor_service",
    "monitor_timer",
    "github_repo",
}
# ADR 0113: an installation either embeds one Claude CLI in each release or uses the store.
_LEGACY_CLAUDE_FIELDS = {"native_claude", "native_sha256"}
_STORE_CLAUDE_FIELDS = {"claude_store", "claude_window"}
_STORE_INPUT_FIELDS = (_INPUT_FIELDS - _LEGACY_CLAUDE_FIELDS) | _STORE_CLAUDE_FIELDS
_UNIT = re.compile(r"[A-Za-z0-9_.@:-]+\.(?:service|timer)\Z")


def store_mode(installation: dict[str, Any]) -> bool:
    return "claude_store" in installation


def load_installation(path: Path) -> dict[str, Any]:
    """Load an exclusively owned private input; reject unsupported or unsafe values."""
    path = Path(path)
    need(
        path.is_absolute() and str(path.resolve()) == str(path),
        "installation path must be canonical",
    )
    info = path.lstat()
    need(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == 0o600,
        "installation must be owned and mode 0600",
    )
    need(
        not any(
            (parent / ".git").is_file() or (parent / ".git/HEAD").is_file()
            for parent in path.parents
        ),
        "installation must be outside Git",
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    need(
        isinstance(data, dict) and set(data) in (_INPUT_FIELDS, _STORE_INPUT_FIELDS),
        "invalid installation fields",
    )
    need(
        type(data["schema_version"]) is int and data["schema_version"] == 1,
        "unsupported installation schema",
    )
    for key in (k for k in _PATH_FIELDS if k in data):
        value = data[key]
        need(
            isinstance(value, str) and value.startswith("/") and "\x00" not in value,
            "invalid path: " + key,
        )
        item = Path(value)
        need(
            str(item) == value and str(item.resolve()) == value and item.exists(),
            "noncanonical or missing path: " + key,
        )
        need(item.stat().st_uid in {0, os.geteuid()}, "unexpected path owner: " + key)
        if key in {"units_dir", "claude_config_dir", "releases_dir", "ops_dir"}:
            need(item.is_dir(), "expected directory: " + key)
        else:
            need(item.is_file(), "expected file: " + key)
    need(
        stat.S_IMODE(Path(data["ops_dir"]).stat().st_mode) & 0o077 == 0,
        "ops directory must be private",
    )
    for key in ("ops_dir", "releases_dir"):
        need(Path(data[key]).stat().st_uid == os.geteuid(), "directory must be user-owned: " + key)
    for key in ("restart_order", "untouched_units", "config_only_units"):
        units = data[key]
        need(
            isinstance(units, list)
            and (bool(units) or key == "config_only_units")
            and all(
                isinstance(u, str) and _UNIT.fullmatch(u) and u.endswith(".service") for u in units
            )
            and len(units) == len(set(units)),
            "invalid units: " + key,
        )
    need(not set(data["restart_order"]) & set(data["untouched_units"]), "overlapping units")
    need(not set(data["restart_order"]) & set(data["config_only_units"]), "overlapping units")
    for key, suffix in (("monitor_service", ".service"), ("monitor_timer", ".timer")):
        value = data[key]
        need(
            isinstance(value, str) and _UNIT.fullmatch(value) and value.endswith(suffix),
            "invalid " + key,
        )
        need(
            value
            not in data["restart_order"] + data["untouched_units"] + data["config_only_units"],
            "overlapping monitor",
        )
    need(
        isinstance(data["controller_unit"], str)
        and data["controller_unit"] == data["restart_order"][-1],
        "controller must be last",
    )
    if store_mode(data):
        value = data["claude_store"]
        need(
            isinstance(value, str) and value.startswith("/") and "\x00" not in value,
            "invalid path: claude_store",
        )
        try:
            claude_store.check_store(Path(value))
            claude_store.in_window(data["claude_window"], time.time())
        except (claude_store.StoreRefused, OSError, TypeError) as exc:
            raise Refused(str(exc)) from None
    else:
        need(
            isinstance(data["native_sha256"], str) and _HEX.fullmatch(data["native_sha256"]),
            "invalid native digest",
        )
    need(
        isinstance(data["github_repo"], str)
        and re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", data["github_repo"])
        and ".." not in data["github_repo"],
        "invalid repository",
    )
    for key in ("claude_config_pattern", "managed_dropin_name"):
        value = data[key]
        need(
            isinstance(value, str)
            and value not in {"", ".", ".."}
            and "/" not in value
            and "\\" not in value,
            "invalid " + key,
        )
    need(not any(c in data["managed_dropin_name"] for c in "*?[]"), "invalid drop-in name")
    return data


def run_command(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, UV_LINK_MODE="copy", PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [str(part) for part in argv],
        check=True,
        timeout=900,
        text=True,
        capture_output=True,
        env=env,
        **kwargs,
    )


def _json_command(runner: Callable[..., Any], argv: list[str]) -> dict[str, Any]:
    result = json.loads(runner([str(part) for part in argv]).stdout)
    need(isinstance(result, dict), "invalid command response")
    return result


def _runtime_command(
    root: Path, action: str, runner: Callable[..., Any], digest: str | None = None
) -> dict[str, Any]:
    argv = [
        str(root / "venv/bin/python"),
        "-I",
        "-B",
        "-m",
        "hermes_codex_router.runtime_release",
        action,
        str(root),
    ]
    if digest is not None:
        argv += ["--sha256", digest]
    result = _json_command(runner, argv)
    need(
        result.get("ok") is True
        and result.get("root") == str(root)
        and isinstance(result.get("sha256"), str)
        and _HEX.fullmatch(result["sha256"])
        and (digest is None or result["sha256"] == digest),
        "runtime verification failed",
    )
    return result


def _applicable_dropin_dirs(unit: str) -> set[str]:
    names = {unit + ".d"}
    stem, suffix = unit.rsplit(".", 1)
    if "@" in stem and not stem.endswith("@"):
        names.add(stem.split("@", 1)[0] + "@." + suffix + ".d")
    return names


def _command_runtime_roots(text: str) -> set[str]:
    roots = set()
    for line in text.splitlines():
        if not line.startswith(("ExecStart=", "ExecStartPost=", "ExecStartPre=", "ExecStopPost=")):
            continue
        try:
            argv = shlex.split(line.partition("=")[2])
        except ValueError as exc:
            raise Refused("invalid ExecStart") from exc
        for arg in argv:
            value = arg.lstrip("-+!:@")
            if "/venv/bin/" in value:
                root = value.split("/venv/bin/", 1)[0]
                if root.startswith("/"):
                    roots.add(root)
    return roots


def _managed_dropins(
    installation: dict[str, Any], expected_root: str | None = None
) -> tuple[str, dict[Path, str]]:
    paths = sorted(
        Path(installation["units_dir"]).glob("*.d/" + installation["managed_dropin_name"])
    )
    texts = {}
    for path in paths:
        content = _file_bytes(path)
        if content is None:
            raise Refused("missing managed drop-in")
        texts[path] = content.decode("utf-8")
    scoped_units = (
        installation["restart_order"]
        + installation["config_only_units"]
        + [installation["monitor_service"]]
    )
    applicable = set().union(*(_applicable_dropin_dirs(unit) for unit in scoped_units))
    protected = set().union(
        *(
            _applicable_dropin_dirs(unit)
            for unit in installation["untouched_units"]
            if unit not in installation["config_only_units"]
        )
    )
    roots = set().union(
        *(
            _command_runtime_roots(text)
            for path, text in texts.items()
            if path.parent.name in applicable
        )
    )
    if expected_root is not None:
        roots.add(expected_root)
    need(bool(roots), "no managed drop-ins")
    need(len(roots) == 1, "mixed runtime roots")
    root = roots.pop()
    held = {path: text for path, text in texts.items() if root in text}
    for path, text in held.items():
        need(path.stat().st_uid in {0, os.geteuid()}, "unsafe managed drop-in")
        need(not (_command_runtime_roots(text) - {root}), "mixed runtime roots")
        need(path.parent.name in applicable, "runtime drop-in has unscoped consumer: " + str(path))
        need(
            path.parent.name not in protected,
            "runtime drop-in has protected consumer: " + str(path),
        )
    for unit in installation["restart_order"] + installation["config_only_units"]:
        need(
            any(path.parent.name in _applicable_dropin_dirs(unit) for path in held),
            "missing managed drop-in: " + unit,
        )
    return root, held


def _unit_runtime_modes(installation: dict[str, Any], root: str) -> dict[str, str]:
    """Classify each unit using its effective managed drop-in (instance wins)."""
    modes = {}
    units_dir = Path(installation["units_dir"])
    for unit in installation["restart_order"]:
        names = _applicable_dropin_dirs(unit)
        specific = unit + ".d"
        ordered = [specific] + sorted(names - {specific})
        paths = [units_dir / name / installation["managed_dropin_name"] for name in ordered]
        path = next((path for path in paths if path.exists() or path.is_symlink()), None)
        if path is None:
            raise Refused("missing managed drop-in: " + unit)
        content = _file_bytes(path)
        if content is None:
            raise Refused("missing managed drop-in: " + unit)
        commands: list[str] = []
        environments: list[str] = []
        for line in content.decode("utf-8").splitlines():
            if line.startswith("ExecStart="):
                value = line.partition("=")[2].strip()
                commands = commands + [value] if value else []
            elif line.startswith("Environment="):
                value = line.partition("=")[2].strip()
                environments = environments + [value] if value else []
        if any(root in command for command in commands):
            modes[unit] = "process"
        else:
            need(
                any(_environment_has_root(value, root) for value in environments),
                "unit has no managed runtime reference: " + unit,
            )
            modes[unit] = "environment"
    return modes


def _environment_has_root(value: str, root: str) -> bool:
    try:
        assignments = shlex.split(value)
    except ValueError as exc:
        raise Refused("invalid service environment") from exc
    return any(
        re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is not None
        and (item == root or item.startswith(root + "/"))
        for name, sep, item in (assignment.partition("=") for assignment in assignments)
        if sep
    )


# A Type=simple unit is active once forked; with PrivateTmp or other sandboxing,
# systemd-executor prepares the namespace before exec, so MainPID is briefly not
# yet the runtime. Residency waits out only that window: up to 150 re-probes,
# 15 s of sleep in total plus probe time.
RESIDENCY_SETTLE_ATTEMPTS = 150
RESIDENCY_SETTLE_INTERVAL = 0.1


def _verify_unit(
    unit: str,
    root: str,
    mode: str,
    manager: Any,
    sleep: Callable[[float], None] = time.sleep,
    *,
    settle: int = RESIDENCY_SETTLE_ATTEMPTS,
) -> dict[str, str]:
    """Return the identity snapshot of an active unit running from ``root``.

    A process-mode unit is probed through the MainPID of one identity snapshot
    and accepted only if the snapshot is unchanged afterwards. While it is active
    but not yet resident, it is re-probed up to ``settle`` times. A restart, a
    state other than ``active`` or an exhausted budget is refused. Pass
    ``settle=0`` from a caller that already retries or must not wait. The
    snapshot is taken here, so a caller that must bind the invocation it
    started compares the returned identity with its own record.
    """
    first = manager.identity(unit)
    if mode != "process":
        need(mode == "environment", "invalid unit runtime mode")
        need(first["ActiveState"] == "active", "unit inactive: " + unit)
        need(
            _environment_has_root(manager.property(unit, "Environment"), root),
            "unit environment does not reference runtime: " + unit,
        )
        need(manager.identity(unit) == first, "unit identity changed while checking: " + unit)
        return first
    need(first["ActiveState"] == "active", "unit inactive or not resident: " + unit)
    attempt = 0
    while True:
        try:
            resident = manager.resident(unit, root, first["MainPID"])
        except (FileNotFoundError, ProcessLookupError):
            resident = False  # The process exited; the identity check decides.
        need(manager.identity(unit) == first, "unit identity changed while starting: " + unit)
        if resident:
            return first
        need(attempt < settle, "unit inactive or not resident: " + unit)
        attempt += 1
        sleep(RESIDENCY_SETTLE_INTERVAL)


def discover_installation(
    installation: dict[str, Any],
    runner: Callable[..., Any],
    inspect_process: Callable[[str, str], bool],
    runtime_owner: Callable[[Path], int] = lambda path: path.stat().st_uid,
    *,
    manager: Any = None,
) -> dict[str, Any]:
    """Inspect fixture or live managed files, with residency established by caller."""
    root, held = _managed_dropins(installation)
    texts = list(held.values())
    root_path = Path(root)
    need(
        str(root_path.resolve()) == root
        and root_path.is_dir()
        and root_path.stat().st_uid in {0, os.geteuid()},
        "invalid runtime root",
    )
    candidates = list(
        Path(installation["claude_config_dir"]).glob(installation["claude_config_pattern"])
    )
    referenced = [p for p in candidates if any(str(p) in text for text in texts)]
    need(
        len(referenced) == 1
        and referenced[0].is_file()
        and not referenced[0].is_symlink()
        and referenced[0].stat().st_uid in {0, os.geteuid()},
        "ambiguous Claude config",
    )
    config = json.loads(referenced[0].read_text(encoding="utf-8"))
    need(isinstance(config, dict), "invalid Claude config")
    agents = config.get("agents")
    permissions = config.get("claude_file_permissions")
    if not isinstance(agents, list) or not isinstance(permissions, dict):
        raise Refused("invalid Claude config")
    claude = [a for a in agents if isinstance(a, dict) and a.get("runtime") == "claude"]
    stable = (
        str(claude_store.stable_path(Path(installation["claude_store"])))
        if store_mode(installation)
        else None
    )
    need(
        bool(claude)
        and all(
            isinstance(a.get("executable"), str)
            and (a["executable"].startswith(root + "/") or a["executable"] == stable)
            for a in claude
        )
        and len({a["executable"] for a in claude}) == 1,
        "mixed Claude runtime",
    )
    # The store serves only the unconfined host profile; confined profiles mount
    # trusted code from sealed release trees (REQ-SEC-008, ADR 0113).
    need(
        not store_mode(installation) or _host_permissions(permissions),
        "Claude CLI store requires the host profile",
    )
    # The host profile (ADR 0095) carries no hook runtime paths or pin.
    if not _host_permissions(permissions):
        for key in ("python_executable", "hook_code_root"):
            value = permissions.get(key)
            need(isinstance(value, str) and value.startswith(root + "/"), "mixed Claude runtime")
        roots = permissions.get("runtime_roots")
        if not (
            isinstance(roots, list)
            and all(isinstance(p, str) and Path(p).is_absolute() for p in roots)
            and any(p == root or p.startswith(root + "/") for p in roots)
        ):
            raise Refused("missing Claude runtime root")
        releases = Path(installation["releases_dir"])
        need(
            all(
                not Path(p).is_relative_to(releases) or p == root or p.startswith(root + "/")
                for p in roots
            ),
            "mixed Claude runtime roots",
        )
        user_owned = (
            root_path.is_relative_to(Path(installation["releases_dir"]))
            or runtime_owner(root_path) != 0
        )
        verify_config_pin(
            config,
            root,
            lambda r, d: bool(_runtime_command(Path(r), "verify", runner, d)),
            user_owned=user_owned,
        )
    modes = _unit_runtime_modes(installation, root)
    for unit, mode in modes.items():
        if mode == "process":
            need(inspect_process(unit, root) is True, "unit inactive or not resident: " + unit)
        else:
            _verify_unit(unit, root, mode, manager or ServiceManager(runner))
    info = _json_command(runner, [str(root_path / "venv/bin/agents-projects-hub"), "release-info"])
    sha = info.get("git_sha")
    need(
        isinstance(sha, str)
        and _SHA.fullmatch(sha)
        and info.get("clean_tree") is True
        and isinstance(info.get("package_version"), str),
        "invalid installed release identity",
    )
    return {
        "root": root,
        "git_sha": sha,
        "package_version": info["package_version"],
        "claude_config": str(referenced[0]),
        "claude_executable": claude[0]["executable"],
        "tree_sha256": (permissions.get("user_releases") or [{}])[0].get("sha256"),
        "dropins": [str(path) for path in held],
        "unit_runtime_modes": modes,
    }


def download_release(installation: dict[str, Any], tag: str, runner: Callable[..., Any]) -> Path:
    need(isinstance(tag, str) and _VERSION.fullmatch(tag), "release must be an exact version tag")
    directory = Path(tempfile.mkdtemp(prefix="release-", dir=installation["ops_dir"]))
    runner(
        [
            "gh",
            "release",
            "download",
            tag,
            "--repo",
            installation["github_repo"],
            "--dir",
            str(directory),
        ]
    )
    return directory


def lookup_release_tag(installation: dict[str, Any], sha: str, runner: Callable[..., Any]) -> str:
    releases = json.loads(
        runner(
            [
                "gh",
                "release",
                "list",
                "--repo",
                installation["github_repo"],
                "--limit",
                "1000",
                "--json",
                "tagName",
            ]
        ).stdout
    )
    need(isinstance(releases, list) and len(releases) < 1000, "release list incomplete")
    matches = []
    for release in releases:
        tag = release.get("tagName") if isinstance(release, dict) else None
        if isinstance(tag, str) and _VERSION.fullmatch(tag):
            commit = _json_command(
                runner, ["gh", "api", f"repos/{installation['github_repo']}/commits/{tag}"]
            )
            if commit.get("sha") == sha:
                matches.append(tag)
    need(len(matches) == 1, "old revision has no unique release tag")
    return matches[0]


def prepare_stage(
    installation: dict[str, Any],
    old: dict[str, Any],
    tag: str,
    runner: Callable[..., Any] = run_command,
    lookup: Callable[..., str] = lookup_release_tag,
    python: Path = Path("/usr/bin/python3.12"),
    rollback_release: str | None = None,
) -> dict[str, Any]:
    """Verify release assets and DB, then prepare inactive runtimes."""
    candidate_dir = download_release(installation, tag, runner)
    candidate, wheel = verify_assets(candidate_dir, tag)
    old_tag = lookup(installation, old["git_sha"], runner)
    old_dir = download_release(installation, old_tag, runner)
    previous, old_wheel = verify_assets(old_dir, old_tag)
    need(
        previous["git_sha"] == old["git_sha"]
        and previous["package_version"] == old["package_version"],
        "old release identity mismatch",
    )
    live = _live_schema(installation)
    rollback_info = None
    rollback_dir = None
    rollback_wheel = None
    if rollback_release is not None:
        need(bool(rollback_release), "empty --rollback-release")
        need(candidate["schema_max"] != live, SAME_SCHEMA_ROLLBACK)
        rollback_dir = download_release(installation, rollback_release, runner)
        rollback_info, rollback_wheel = verify_assets(rollback_dir, rollback_release)
    check_schema(previous, candidate, live, rollback_info)
    if rollback_info is not None:
        need(
            rollback_info["git_sha"] not in {old["git_sha"], candidate["git_sha"]},
            "rollback release must be distinct",
        )
    prepared = _prepare_runtime(installation, candidate, candidate_dir, wheel, runner, python)
    if store_mode(installation):
        prepared["claude_executable"] = prepare_claude_store(installation, old, prepared)
    prepared.update(
        {
            "state_schema_before": live,
            "old_wheel": str(old_wheel),
            "new_wheel": str(wheel),
            "old_wheel_sha256": _wheel_digest(previous, old_wheel),
            "new_wheel_sha256": _wheel_digest(candidate, wheel),
            "candidate_assets": str(candidate_dir),
            "old_assets": str(old_dir),
        }
    )
    if rollback_info is None and live >= 59:
        prepared["old_" + DECLARATION] = previous[DECLARATION]
    if rollback_info is not None:
        assert rollback_dir is not None and rollback_wheel is not None
        target = _prepare_runtime(
            installation, rollback_info, rollback_dir, rollback_wheel, runner, python
        )
        if store_mode(installation):
            target["claude_executable"] = prepare_claude_store(installation, old, target)
        target.update(
            {
                "old_wheel": prepared["old_wheel"],
                "new_wheel": str(rollback_wheel),
                "old_wheel_sha256": prepared["old_wheel_sha256"],
                "new_wheel_sha256": _wheel_digest(rollback_info, rollback_wheel),
                "candidate_assets": str(rollback_dir),
                "state_schema_before": live,
                "tag": rollback_release,
            }
        )
        prepared["rollback"] = target
    return prepared


def claude_cli_version(executable: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="hub-claude-version-") as home:
        output = subprocess.run(
            [str(executable), "--version"],
            env={"PATH": "/usr/bin:/bin", "HOME": home, "DISABLE_AUTOUPDATER": "1"},
            cwd=home,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
            stdin=subprocess.DEVNULL,
        ).stdout
    match = re.fullmatch(r"([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}) \(Claude Code\)", output.strip())
    need(match is not None, "unrecognized Claude CLI version")
    assert match is not None
    return match.group(1)


def prepare_claude_store(
    installation: dict[str, Any],
    old: dict[str, Any],
    new: dict[str, Any],
    *,
    now: Callable[[], float] = time.time,
) -> str:
    """Seed the store once and require gate evidence for the new release (ADR 0113)."""
    root = Path(new["root"])
    gate = claude_store.release_gate(root)
    check = claude_store.release_post_check(root)
    try:
        store = claude_store.open_store(Path(installation["claude_store"]))
        # An unfinished switch belongs to the active release; settle it with its checker.
        if old["claude_executable"] == str(claude_store.stable_path(store)):
            claude_store.recover(
                store, claude_store.release_post_check(Path(old["root"])), now=now()
            )
        if claude_store.current_id(store) is None:
            executable = Path(old["claude_executable"])
            need(
                executable == Path(old["root"]) / "bin/claude",
                "store seed requires the active release's embedded Claude CLI",
            )
            receipt = claude_store.seed(
                store, executable, claude_cli_version(executable), old["git_sha"], now=now()
            )
            gated = claude_store.run_pair_gate(
                store, gate, new["git_sha"], receipt, None, now=now()
            )
            need(gated["passed"], "release failed the gate for the seeded Claude CLI")
            claude_store.first_selection(
                store, receipt, hub_git_sha=new["git_sha"], post_check=check, now=now()
            )
        else:
            claude_store.ensure_pair(store, gate, new["git_sha"], now=now())
    except claude_store.StoreRefused as exc:
        raise Refused(str(exc)) from None
    except claude_store.NeedsInspection as exc:
        raise Refused("Claude CLI store needs inspection: " + str(exc)) from None
    return str(claude_store.stable_path(store))


def _prepare_runtime(
    installation: dict[str, Any],
    candidate: dict[str, Any],
    candidate_dir: Path,
    wheel: Path,
    runner: Callable[..., Any],
    python: Path,
) -> dict[str, Any]:
    embedded = not store_mode(installation)
    native = Path(installation.get("native_claude", "/nonexistent"))
    need(
        not embedded
        or hashlib.sha256(native.read_bytes()).hexdigest() == installation["native_sha256"],
        "native Claude digest mismatch",
    )
    root = Path(installation["releases_dir"]) / candidate["git_sha"]
    if root.exists() or root.is_symlink():
        need(root.is_dir() and not root.is_symlink(), "unsafe existing runtime")
        pin = _runtime_command(root, "pin", runner)
        _runtime_command(root, "verify", runner, pin["sha256"])
        info = _json_command(runner, [str(root / "venv/bin/agents-projects-hub"), "release-info"])
        identity_keys = ("git_sha", "clean_tree", "package_version") + (
            ("explicit_artifact_loss_declaration",) if candidate.get("schema_max", 0) >= 59 else ()
        )
        need(
            all(info.get(k) == candidate[k] for k in identity_keys),
            "existing runtime identity mismatch",
        )
        if embedded:
            need(
                hashlib.sha256((root / "bin/claude").read_bytes()).hexdigest()
                == installation["native_sha256"],
                "existing native Claude digest mismatch",
            )
        else:
            need(not (root / "bin/claude").exists(), "store release embeds a Claude CLI")
    else:
        root.mkdir(mode=0o755)
        runtime_python = root / "venv/bin/python"
        runner(["uv", "venv", "--python", str(python), str(root / "venv")])
        runner(
            [
                "uv",
                "pip",
                "sync",
                "--python",
                str(runtime_python),
                "--require-hashes",
                str(candidate_dir / "requirements-release.lock"),
            ]
        )
        runner(["uv", "pip", "install", "--python", str(runtime_python), "--no-deps", str(wheel)])
        for name in ("python", "python3", "python3.12"):
            path = root / "venv/bin" / name
            need(path.is_symlink(), "unexpected interpreter layout")
            path.unlink()
        shutil.copyfile(python, root / "venv/bin/python3.12")
        (root / "venv/bin/python3.12").chmod(0o755)
        for name in ("python", "python3"):
            (root / "venv/bin" / name).symlink_to("python3.12")
        if embedded:
            (root / "bin").mkdir(mode=0o755)
            shutil.copyfile(native, root / "bin/claude")
            (root / "bin/claude").chmod(0o755)
        for cache in root.rglob("__pycache__"):
            shutil.rmtree(cache)
        info = _json_command(runner, [str(root / "venv/bin/agents-projects-hub"), "release-info"])
        identity_keys = ("git_sha", "clean_tree", "package_version") + (
            ("explicit_artifact_loss_declaration",) if candidate.get("schema_max", 0) >= 59 else ()
        )
        need(
            all(info.get(k) == candidate[k] for k in identity_keys),
            "built runtime identity mismatch",
        )
        for cache in root.rglob("__pycache__"):
            shutil.rmtree(cache)
        pin = _runtime_command(root, "seal", runner)
        _runtime_command(root, "verify", runner, pin["sha256"])
    return {
        "root": str(root),
        "git_sha": candidate["git_sha"],
        "tree_sha256": pin["sha256"],
        "schema_max": candidate["schema_max"],
        "package_version": candidate["package_version"],
        "explicit_artifact_loss_declaration": candidate.get("explicit_artifact_loss_declaration"),
    }


def _file_bytes(path: Path) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        return None
    need(path.is_file() and not path.is_symlink(), "unsafe file: " + str(path))
    need(str(path.resolve()) == str(path), "noncanonical file: " + str(path))
    return path.read_bytes()


def _templates(prepared: dict[str, Any]) -> dict[str, bytes]:
    assets = Path(prepared["candidate_assets"])
    archives = list(assets.glob("agents_projects_hub-*.tar.gz"))
    need(len(archives) == 1, "missing release archive")
    templates: dict[str, bytes] = {}
    with tarfile.open(archives[0], "r:gz") as archive:
        for name in BASE_UNITS:
            matches = [m for m in archive.getmembers() if m.name.endswith("/systemd/" + name)]
            need(len(matches) == 1 and matches[0].isfile(), "missing or ambiguous unit template")
            stream = archive.extractfile(matches[0])
            need(stream is not None, "unreadable unit template")
            assert stream is not None
            templates[name] = stream.read()
    return templates


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _private_bytes(path: Path) -> bytes:
    content = _file_bytes(path)
    need(
        content is not None
        and path.stat().st_uid == os.geteuid()
        and stat.S_IMODE(path.stat().st_mode) == 0o600,
        "unsafe private plan file",
    )
    assert content is not None
    return content


def _check_candidate_unit(before: bytes, after: bytes) -> None:
    # Preserve the Unit section except ordering directives. Group continuations
    # before filtering so a continued dependency cannot masquerade as ordering.
    def relevant(content: bytes) -> list[str]:
        section = ""
        result = []
        logical_lines: list[str] = []
        pending = ""
        for line in content.decode("utf-8").splitlines():
            pending += line + "\n"
            if not line.rstrip().endswith("\\"):
                logical_lines.append(pending)
                pending = ""
        need(not pending, "unterminated candidate unit continuation")
        for line in logical_lines:
            stripped = line.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped
            if section == "[Unit]" and line.partition("=")[0].strip() in {"After", "Before"}:
                continue
            if section == "[Unit]" or (
                section == "[Service]" and line.partition("=")[0].strip() == "Sockets"
            ):
                result.append(line)
        return result

    need(relevant(before) == relevant(after), "candidate unit dependency configuration changed")


def create_plan(
    installation: dict[str, Any],
    old: dict[str, Any],
    new: dict[str, Any],
    runner: Callable[..., Any],
) -> Path:
    """Generate private evidence without changing the installed files."""
    old_config = Path(old["claude_config"])
    old_name = old_config.name
    short = old["git_sha"][:7]
    need(old_name.count(short) <= 1, "ambiguous Claude config name")
    new_name = old_name.replace(short, new["git_sha"][:7]) if short in old_name else old_name
    need(short not in old_name or new_name != old_name, "candidate config name is unchanged")
    config_bytes = _file_bytes(old_config)
    if config_bytes is None:
        raise Refused("missing Claude config")
    config = json.loads(config_bytes)
    candidate_config = rewrite_claude_config(
        config,
        old["root"],
        new["root"],
        new["tree_sha256"],
        claude_executable=new.get("claude_executable"),
    )
    manifest_path = Path(installation["codex_manifest"])
    manifest_bytes = _file_bytes(manifest_path)
    if manifest_bytes is None:
        raise Refused("missing Codex manifest")
    manifest = json.loads(manifest_bytes)
    need(
        isinstance(manifest, dict) and manifest.get("hub_git_sha") == old["git_sha"],
        "manifest revision mismatch",
    )
    manifest["hub_git_sha"] = new["git_sha"]

    def encode(value: object) -> bytes:
        return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode()

    hub_path = Path(installation["hub_config"])
    hub_bytes = _file_bytes(hub_path)
    if hub_bytes is None:
        raise Refused("missing Hub config")
    # ADR 0104: the first boot start reconciles with this installation's Hub config.
    manifest["hub_config"] = str(hub_path)
    need(
        claude_policy(json.loads(hub_bytes)) == claude_policy(candidate_config),
        "Claude settings differ between the Hub config and the Claude worker config",
    )
    target_config = old_config.with_name(new_name)
    files: list[tuple[str, Path, bytes | None, bytes | None]] = [
        ("hub_config", hub_path, hub_bytes, hub_bytes),
        (
            "claude_config",
            target_config,
            config_bytes if target_config == old_config else _file_bytes(target_config),
            encode(candidate_config),
        ),
        ("manifest", manifest_path, manifest_bytes, encode(manifest)),
    ]
    if target_config != old_config:
        files.insert(0, ("source_config", old_config, config_bytes, config_bytes))
    units_dir = Path(installation["units_dir"])
    _, held = _managed_dropins(installation, old["root"])
    need([str(path) for path in held] == old["dropins"], "managed drop-in set changed")
    modes = _unit_runtime_modes(installation, old["root"])
    need(modes == old["unit_runtime_modes"], "unit runtime references changed")
    for path, text in held.items():
        if path.parent.name.endswith("@.service.d"):
            _check_template_instances(path.parent.name[:-2], installation["restart_order"], runner)
        after = rewrite_dropin(text, old["root"], new["root"], old_name, new_name)
        _check_candidate_unit(text.encode(), after.encode())
        files.append(("dropin", path, text.encode(), after.encode()))
    scope_dirs = set().union(
        *(
            _applicable_dropin_dirs(unit)
            for unit in installation["restart_order"] + [installation["monitor_service"]]
        )
    )
    for unit, content in _templates(new).items():
        path = units_dir / unit
        before = _file_bytes(path)
        if unit in installation["untouched_units"] or unit in BASE_UNITS[-2:]:
            need(before == content, "protected unit template changed: " + unit)
        if unit + ".d" not in scope_dirs:
            content = before
        if unit.endswith("@.service"):
            content = before
        if before is not None and content is not None:
            _check_candidate_unit(before, content)
        files.append(("base_unit" if before != content else "base_guard", path, before, content))
    need(len({str(path) for _, path, _, _ in files}) == len(files), "duplicate managed file")
    plan_dir = Path(tempfile.mkdtemp(prefix="plan-", dir=installation["ops_dir"]))
    try:
        entries = []
        for index, (kind, path, before, after) in enumerate(files):
            if kind in {
                "source_config",
                "hub_config",
                "dropin",
                "base_unit",
                "base_guard",
                "manifest",
            }:
                need(before is not None, "missing managed file: " + str(path))
            entry: dict[str, Any] = {
                "kind": kind,
                "path": str(path),
                "changed": before != after,
                "after_mode": 0o600
                if kind in {"claude_config", "manifest"}
                else (
                    0o644
                    if kind in {"base_unit", "dropin"}
                    else (stat.S_IMODE(path.stat().st_mode) if before is not None else 0o600)
                ),
                "mode": stat.S_IMODE(path.stat().st_mode)
                if before is not None
                else (0o600 if kind in {"claude_config", "manifest"} else 0o644),
            }
            for label, content in (("before", before), ("after", after)):
                if content is None:
                    entry[label] = None
                else:
                    name = f"{index:03d}-{label}"
                    _write_private(plan_dir / name, content)
                    entry[label] = {"copy": name, "sha256": hashlib.sha256(content).hexdigest()}
            entries.append(entry)
        old_runtime = {k: old[k] for k in ("root", "git_sha", "tree_sha256")}
        if "old_" + DECLARATION in new:
            # The same-schema rollback capability, from verified release metadata.
            old_runtime[DECLARATION] = new["old_" + DECLARATION]
        plan = {
            "schema_version": 1,
            "installation": copy.deepcopy(installation),
            "state_schema": new["schema_max"],
            "state_schema_before": new.get("state_schema_before", new["schema_max"]),
            "old_wheel": new["old_wheel"],
            "new_wheel": new["new_wheel"],
            "old_wheel_sha256": _bound_digest(new["old_wheel"], new["old_wheel_sha256"]),
            "new_wheel_sha256": _bound_digest(new["new_wheel"], new["new_wheel_sha256"]),
            "old_runtime": old_runtime,
            "new_runtime": {k: new[k] for k in ("root", "git_sha", "tree_sha256")},
            "restart_order": installation["restart_order"],
            "unit_runtime_modes": modes,
            "untouched_units": installation["untouched_units"],
            "config_only_units": installation["config_only_units"],
            "candidate_config": str(target_config),
            "files": entries,
        }
        _validate_plan_configs(plan_dir, plan, "new_runtime", "after", "candidate_config", runner)
        if "rollback" in new:
            _add_rollback_plan(plan_dir, plan, installation, old, new["rollback"], runner)
        _write_private(plan_dir / "plan.json", canonical_document(bind_plan(plan)) + b"\n")
        verify_saved_plan(plan_dir)
        _sync_dir(plan_dir)
        _sync_dir(plan_dir.parent)
        return plan_dir
    except Exception:
        shutil.rmtree(plan_dir)
        raise


def _add_rollback_plan(
    plan_dir: Path,
    plan: dict[str, Any],
    installation: dict[str, Any],
    old: dict[str, Any],
    target: dict[str, Any],
    runner: Callable[..., Any],
) -> None:
    other_dir = create_plan(installation, old, target, runner)
    try:
        other = verify_saved_plan(other_dir)
        entries = {entry["path"]: entry for entry in plan["files"]}
        for item in other["files"]:
            entry = entries.get(item["path"])
            if entry is None:
                entry = copy.deepcopy(item)
                entry["after"] = entry["before"]
                plan["files"].append(entry)
                entries[item["path"]] = entry
                if entry["before"] is not None:
                    raise Refused("rollback file inventory differs unexpectedly")
            else:
                need(
                    entry["before"] == item["before"]
                    or (
                        entry["before"] is not None
                        and item["before"] is not None
                        and entry["before"]["sha256"] == item["before"]["sha256"]
                        and entry["mode"] == item["mode"]
                    ),
                    "rollback file inventory changed",
                )
            snapshot = item["after"]
            if snapshot is not None:
                data = _private_bytes(other_dir / snapshot["copy"])
                name = f"rollback-{len([p for p in plan_dir.glob('rollback-*')]):03d}"
                _write_private(plan_dir / name, data)
                snapshot = {"copy": name, "sha256": hashlib.sha256(data).hexdigest()}
            entry["rollback"] = snapshot
            entry["rollback_mode"] = item["after_mode"]
        for entry in plan["files"]:
            if "rollback" not in entry:
                entry["rollback"] = entry["before"]
                entry["rollback_mode"] = entry["mode"]
        plan["rollback_config"] = other["candidate_config"]
        plan["rollback_runtime"] = {
            k: target[k] for k in ("root", "git_sha", "tree_sha256", "tag", "package_version")
        }
        plan["rollback_wheel"] = target["new_wheel"]
        plan["rollback_wheel_sha256"] = _bound_digest(
            target["new_wheel"], target["new_wheel_sha256"]
        )
    finally:
        shutil.rmtree(other_dir)


def _validate_plan_configs(
    plan_dir: Path,
    plan: dict[str, Any],
    runtime: str,
    snapshot: str,
    config_path: str,
    runner: Callable[..., Any],
) -> None:
    for kind, path in (
        ("hub_config", plan["installation"]["hub_config"]),
        ("claude_config", plan[config_path]),
    ):
        matches = [e for e in plan["files"] if e["kind"] == kind and e["path"] == path]
        need(
            len(matches) == 1 and matches[0].get(snapshot) is not None,
            "missing active config snapshot: " + config_path,
        )
        saved = matches[0][snapshot]
        result = _json_command(
            runner, [_runtime_cli(plan[runtime]), "validate-hub", str(plan_dir / saved["copy"])]
        )
        need(result.get("ok") is True, "config validation failed: " + config_path)


def verify_saved_plan(plan_dir: Path, *, check_live: bool = True) -> dict[str, Any]:
    """Recompute the plan and every stored and live file digest."""
    need(plan_dir.is_dir() and not plan_dir.is_symlink(), "invalid plan directory")
    need(
        plan_dir.stat().st_uid == os.geteuid() and stat.S_IMODE(plan_dir.stat().st_mode) == 0o700,
        "plan directory must be private",
    )
    bound = json.loads(_private_bytes(plan_dir / "plan.json"))
    plan = verify_plan(bound)
    need(isinstance(plan.get("files"), list), "invalid plan files")
    for entry in plan["files"]:
        for label in ("before", "after", *(["rollback"] if "rollback" in entry else [])):
            snapshot = entry[label]
            if snapshot is not None:
                name = snapshot["copy"]
                need(
                    isinstance(name, str)
                    and re.fullmatch(r"(?:[0-9]{3}-(?:before|after)|rollback-[0-9]{3})", name),
                    "invalid snapshot",
                )
                content = _private_bytes(plan_dir / name)
                need(
                    hashlib.sha256(content).hexdigest() == snapshot["sha256"],
                    "snapshot digest mismatch",
                )
        if not check_live:
            continue
        live = _file_bytes(Path(entry["path"]))
        before = entry["before"]
        need(
            (live is None and before is None)
            or (
                live is not None
                and before is not None
                and hashlib.sha256(live).hexdigest() == before["sha256"]
            ),
            "live file changed since planning",
        )
        if live is not None:
            need(
                stat.S_IMODE(Path(entry["path"]).stat().st_mode) == entry["mode"],
                "live file mode changed since planning",
            )
    return plan


def _process_resident(pid: str, root: str) -> bool:
    """Whether process ``pid`` has an argument under the runtime ``root``."""
    if not pid.isdecimal() or int(pid) <= 0:
        return False
    args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    prefix = os.fsencode(root + "/")
    return any(arg.startswith(prefix) for arg in args)


def inspect_unit(unit: str, root: str, runner: Callable[..., Any]) -> bool:
    identity = ServiceManager(runner).identity(unit)
    return identity["ActiveState"] == "active" and _process_resident(identity["MainPID"], root)


def _unit_list(manager: Any, unit: str, name: str) -> list[str]:
    """Split a unit-list property; systemctl quotes escaped names such as slices."""
    try:
        return shlex.split(manager.property(unit, name))
    except ValueError:
        raise Refused(f"malformed {name} property of {unit}") from None


class ServiceManager:
    def __init__(self, runner: Callable[..., Any]):
        self.runner = runner

    def property(self, unit: str, name: str) -> str:
        if name in (*_DEPENDENCIES, "Id", "Names"):
            output = self.runner(
                # "--" keeps unit names such as the root "-.slice" from parsing as options.
                ["systemctl", "--user", "show", "--all", "--property=" + name, "--", unit]
            ).stdout.strip()
            prefix = name + "="
            need(
                output.startswith(prefix) and "\n" not in output,
                "missing dependency property: " + name,
            )
            return output[len(prefix) :]
        return self.runner(
            ["systemctl", "--user", "show", "--property=" + name, "--value", "--", unit]
        ).stdout.strip()

    def identity(self, unit: str) -> dict[str, str]:
        """Read the three values in one call, so they form one manager snapshot."""
        keys = ("ActiveState", "MainPID", "InvocationID")
        output = self.runner(
            # --all keeps an inactive unit's empty InvocationID.
            ["systemctl", "--user", "show", "--all", "--property=" + ",".join(keys), "--", unit]
        ).stdout
        lines = [line.partition("=") for line in output.splitlines()]
        values = {key: value for key, sep, value in lines if sep}
        need(
            len(lines) == len(keys) == len(values) and set(values) == set(keys),
            "malformed identity of " + unit,
        )
        return {key: values[key] for key in keys}

    def enabled(self, unit: str) -> bool:
        return self.property(unit, "UnitFileState") == "enabled"

    def executable(self, unit: str) -> str:
        pid = self.property(unit, "MainPID")
        need(pid.isdecimal() and int(pid) > 0, "invalid app-server PID")
        return os.readlink("/proc/" + pid + "/exe")

    def preflight_extra(self) -> None:
        need(
            self.property("agents-projects-hub-codex-app-server-oneoff.service", "ActiveState")
            == "inactive",
            "fallback app-server is running",
        )

    def verify_effective(self, app: str) -> None:
        need(self.enabled(app), "managed app-server is not enabled")
        need(
            self.property(app, "TimeoutStartUSec") == "5min",
            "managed app-server start timeout differs",
        )
        socket_dir = "agents-projects-hub-codex-socket-dir.service"
        need(self.property(socket_dir, "ActiveState") != "failed", "socket directory unit failed")
        need(
            self.property(socket_dir, "StartLimitIntervalUSec") == "0",
            "socket directory start limit differs",
        )

    def stop(self, unit: str) -> None:
        self.runner(["systemctl", "--user", "stop", unit])

    def start(self, unit: str) -> None:
        self.runner(["systemctl", "--user", "start", unit])

    def reload(self) -> None:
        self.runner(["systemctl", "--user", "daemon-reload"])

    def resident(self, unit: str, root: str, pid: str) -> bool:
        """Probe ``pid`` from the caller's identity snapshot, never a new lookup."""
        return _process_resident(pid, root)


_DEPENDENCIES = (
    "Wants",
    "Requires",
    "Requisite",
    "BindsTo",
    "PartOf",
    "Upholds",
    "WantedBy",
    "RequiredBy",
    "RequisiteOf",
    "BoundBy",
    "ConsistsOf",
    "UpheldBy",
    "PropagatesStopTo",
    "StopPropagatedFrom",
    "Conflicts",
    "ConflictedBy",
)


def _rerun_helper(manager: Any, unit: str) -> bool:
    """A oneshot without RemainAfterExit is re-run by every dependent start."""
    return (
        manager.property(unit, "Type") == "oneshot"
        and manager.property(unit, "RemainAfterExit") == "no"
    )


def _check_dependencies(
    manager: Any, operated: list[str], untouched: list[str], config_only: tuple[str, ...] = ()
) -> None:
    """Follow effective systemd start/stop propagation, refusing protected effects.

    A helper is a protected config-only oneshot without RemainAfterExit: every
    dependent start re-runs it by design, so Wants/Requires/BindsTo may start it.
    Stop, Upholds and any identity shared with another protected unit still refuse.
    """
    protected: set[str] = set()
    cache: dict[str, dict[str, list[str]]] = {}
    aliases: dict[str, set[str]] = {}

    def read(unit: str) -> dict[str, list[str]]:
        if unit not in cache:
            need(len(cache) < 2048, "dependency graph too large")
            identity = manager.property(unit, "Id")
            names = set(_unit_list(manager, unit, "Names"))
            need(identity in names and unit in names, "missing dependency unit identity: " + unit)
            aliases[unit] = names
            values = {name: _unit_list(manager, unit, name) for name in _DEPENDENCIES}
            need(
                all(
                    re.fullmatch(r"[A-Za-z0-9_.@:\\-]+", target)
                    for targets in values.values()
                    for target in targets
                ),
                "invalid dependency unit name",
            )
            cache[unit] = values
        return cache[unit]

    for unit in untouched:
        read(unit)
        protected.update(aliases[unit])
    strict = set(protected)
    for unit in config_only:
        read(unit)
        protected.update(aliases[unit])
    helper_ids: set[str] = set()
    for unit in config_only:
        read(unit)
        if not aliases[unit] & strict and _rerun_helper(manager, unit):
            helper_ids.update(aliases[unit])
    pending = [(unit, action, "") for unit in operated for action in ("start", "stop")]
    visited: set[tuple[str, str, str]] = set()
    while pending:
        unit, action, edge = pending.pop()
        if (unit, action, edge) in visited:
            continue
        visited.add((unit, action, edge))
        values = read(unit)
        names = aliases[unit]
        need(
            not names & protected
            or (
                action == "start"
                and edge in ("Wants", "Requires", "BindsTo")
                and names <= helper_ids
            ),
            "dependency can operate on untouched unit: " + unit,
        )
        if action == "start":
            for name in ("Wants", "Requires", "BindsTo", "Upholds"):
                pending.extend((target, "start", name) for target in values[name])
            for name in ("Conflicts", "ConflictedBy"):
                pending.extend((target, "stop", name) for target in values[name])
        else:
            for name in ("RequiredBy", "BoundBy", "ConsistsOf", "PropagatesStopTo"):
                pending.extend((target, "stop", name) for target in values[name])
            if values["UpheldBy"]:
                pending.append((unit, "start", "UpheldBy"))


def _check_template_instances(
    template: str, restart_order: list[str], runner: Callable[..., Any]
) -> None:
    prefix = template.replace("@.service", "@")
    result = runner(
        [
            "systemctl",
            "--user",
            "list-units",
            "--all",
            "--type=service",
            "--no-legend",
            "--plain",
            "--no-pager",
            prefix + "*.service",
        ]
    )
    for line in result.stdout.splitlines():
        fields = line.split()
        need(len(fields) >= 4, "invalid template instance listing")
        unit, _, state = fields[:3]
        need(
            unit.startswith(prefix) and unit.endswith(".service") and _UNIT.fullmatch(unit),
            "invalid template instance listing",
        )
        if state not in {"inactive", "failed"}:
            need(
                unit in restart_order,
                "shared runtime drop-in has unscoped active instance: " + unit,
            )


def _verify_config_only(
    plan: dict[str, Any],
    manager: Any,
    untouched: dict[str, Any],
    role: str,
    *,
    started: bool = False,
) -> None:
    root = plan[role]["root"]
    others = {
        plan[key]["root"]
        for key in ("old_runtime", "new_runtime", "rollback_runtime")
        if key in plan
    } - {root}
    for unit in plan["config_only_units"]:
        if unit not in plan["untouched_units"] and _rerun_helper(manager, unit):
            # Dependent starts re-run it, so its identity changes. Before starts only its
            # configuration is checked; after starts it must have finished successfully.
            need(
                not started
                or (
                    manager.property(unit, "ActiveState") == "inactive"
                    and manager.property(unit, "SubState") == "dead"
                    and manager.property(unit, "Result") == "success"
                ),
                "config-only helper not finished successfully: " + unit,
            )
        else:
            need(
                manager.identity(unit) == untouched["units"][unit],
                "config-only unit changed: " + unit,
            )
        required = set()
        for entry in plan["files"]:
            path = Path(entry["path"])
            if entry["kind"] != "dropin" or path.parent.name not in _applicable_dropin_dirs(unit):
                continue
            for line in path.read_text().splitlines():
                key, sep, value = line.partition("=")
                if sep and key in {"ExecStart", "ExecStartPost", "Environment"} and root in value:
                    required.add(key)
        need(bool(required), "config-only unit has no runtime reference: " + unit)
        for key in ("ExecStart", "ExecStartPost", "Environment"):
            value = manager.property(unit, key)
            need(
                all(other not in value for other in others),
                "config-only unit has stale runtime: " + unit,
            )
            if key in required:
                has_root = (
                    _environment_has_root(value, root)
                    if key == "Environment"
                    else re.search(
                        r"(?<![A-Za-z0-9_./-])" + re.escape(root) + r"(?=/|[\s\"';}]|$)", value
                    )
                    is not None
                )
                need(has_root, "config-only unit effective runtime mismatch: " + unit)


def _check_window_dependencies(plan: dict[str, Any], manager: Any) -> None:
    installation = plan["installation"]
    _check_dependencies(
        manager,
        [*plan["restart_order"], installation["monitor_service"], installation["monitor_timer"]],
        list(plan["untouched_units"]),
        tuple(plan["config_only_units"]),
    )
    for entry in plan["files"]:
        path = Path(entry["path"])
        if entry["kind"] == "dropin" and path.parent.name.endswith("@.service.d"):
            _check_template_instances(path.parent.name[:-2], plan["restart_order"], manager.runner)


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_file(path: Path, data: bytes | None, mode: int) -> None:
    if data is None:
        path.unlink(missing_ok=True)
        _sync_dir(path.parent)
        return
    fd, name = tempfile.mkstemp(prefix=".hub-deploy-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        _sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def _lock(installation: dict[str, Any]):
    path = Path(installation["ops_dir"]) / "deploy.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _journal(window: Path, phase: str, **extra: Any) -> None:
    path = window / "journal.jsonl"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        data = stream.read()
        if data and not data.endswith(b"\n"):
            end = data.rfind(b"\n") + 1
            need(end > 0, "journal has no complete record")
            _atomic_file(window / ("journal-torn-" + str(time.time_ns())), data[end:], 0o600)
            stream.truncate(end)
            stream.flush()
            os.fsync(stream.fileno())
        stream.seek(0, os.SEEK_END)
        stream.write(canonical_document({"phase": phase, **extra}) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    _sync_dir(window)


def _journal_rows(path: Path) -> list[dict[str, Any]]:
    data = _private_bytes(path)
    # A process or power loss may leave a partial final append. Earlier durable
    # records remain authoritative; never interpret a torn record as completion.
    if data and not data.endswith(b"\n"):
        data = data.rsplit(b"\n", 1)[0] + b"\n" if b"\n" in data else b""
    return [json.loads(line) for line in data.splitlines()]


def last_window(installation: dict[str, Any]) -> dict[str, Any] | None:
    windows = sorted(Path(installation["ops_dir"]).glob("window-*"), key=lambda p: p.name)
    if not windows:
        return None
    path = windows[-1]
    rows = _journal_rows(path / "journal.jsonl")
    need(bool(rows), "empty window journal")
    return {"id": path.name, "state": rows[-1]["phase"]}


def window_status(installation: dict[str, Any]) -> dict[str, Any] | None:
    result = last_window(installation)
    if result is None:
        return None
    path = Path(installation["ops_dir"]) / result["id"]
    metadata = json.loads(_private_bytes(path / "metadata.json"))
    plan_dir = Path(metadata["plan_dir"])
    plan = verify_saved_plan(plan_dir, check_live=False)
    need(
        json.loads(_private_bytes(plan_dir / "plan.json"))["sha256"] == metadata["plan_sha256"],
        "window plan changed",
    )
    need(plan["installation"] == installation, "installation changed")
    schema = _live_schema(installation)
    before = plan.get("state_schema_before", plan["state_schema"])
    target = (
        "previous"
        if schema == before
        else "rollback_release"
        if schema == plan["state_schema"] and "rollback_runtime" in plan
        else "owner_required"
    )
    result.update({"schema": schema, "rollback_target": target})
    if target != "owner_required":
        role = "old_runtime" if target == "previous" else "rollback_runtime"
        result["rollback_git_sha"] = plan[role]["git_sha"]
    amendments = [
        {
            "id": item.name,
            "state": _amendment_state(item),
            "changed_keys": _amendment_metadata(item)["changed_keys"],
        }
        for item in _amendments(path)
    ]
    result["amendments"] = amendments
    if result["state"] in AMENDMENT_EFFECTIVE and all(
        a["state"] in AMENDMENT_DONE for a in amendments
    ):
        # The manifest that binds the installed Hub config, unless it was hand-edited.
        head, _ = _amendment_chain(path, plan)
        live = _file_bytes(Path(installation["hub_config"]))
        effective = [a["id"] for a in amendments if a["state"] in AMENDMENT_EFFECTIVE]
        manifest = path / (effective[-1] if effective else "") / MANIFEST
        if live is None or hashlib.sha256(live).hexdigest() != head:
            result["config_drift"] = True
        elif manifest.is_file():
            result["manifest"] = str(manifest)
    return result


def _unfinished(installation: dict[str, Any], input_path: str | None = None) -> None:
    suffix = "" if input_path is None else " --installation " + shlex.quote(input_path)
    for path in sorted(Path(installation["ops_dir"]).glob("window-*")):
        rows = _journal_rows(path / "journal.jsonl")
        if not rows or rows[-1]["phase"] not in WINDOW_DONE:
            command = "python3.12 -I scripts/hub_deploy.py rollback --window " + path.name
            if any(_amendment_state(item) != "REVERTED" for item in _amendments(path)):
                command += " --drop-amendments"
            raise Refused("unfinished window; run " + command + suffix)
        # A release rollback drops the window's amendments, finished or not.
        if rows[-1]["phase"] != "ROLLED_BACK" and any(
            _amendment_state(item) not in AMENDMENT_DONE for item in _amendments(path)
        ):
            command = "python3.12 -I scripts/hub_deploy.py reconfigure --recover"
            raise Refused("unfinished config amendment; run " + command + suffix)


def _busy(installation: dict[str, Any]) -> list[Any]:
    with closing(
        sqlite3.connect(Path(installation["state_db"]).as_uri() + "?mode=ro", uri=True)
    ) as db:
        return db.execute(
            "SELECT agent_id,status,count(*) FROM provider_jobs WHERE status IN ('queued','leased','executing','retry_wait') GROUP BY 1,2"
        ).fetchall()


def _runtime_cli(runtime: dict[str, Any]) -> str:
    return str(Path(runtime["root"]) / "venv/bin/agents-projects-hub")


def _socket(
    runner: Callable[..., Any], runtime: dict[str, Any], installation: dict[str, Any]
) -> str:
    try:
        data = _json_command(runner, [_runtime_cli(runtime), "doctor", installation["hub_config"]])
    except subprocess.CalledProcessError as exc:
        need(isinstance(exc.stdout, str), "doctor did not return a report")
        data = json.loads(exc.stdout)
        need(isinstance(data, dict), "invalid doctor response")
    checks = data.get("checks")
    if not isinstance(checks, list):
        raise Refused("invalid doctor checks")
    matches = [row for row in checks if isinstance(row, dict) and row.get("name") == "codex_socket"]
    need(
        len(matches) == 1 and isinstance(matches[0].get("detail"), str),
        "missing or ambiguous socket check",
    )
    return matches[0]["detail"].split(":", 1)[0]


def _verify(
    runtime: dict[str, Any],
    plan: dict[str, Any],
    manager: Any,
    runner: Callable[..., Any],
    untouched: dict[str, Any],
    sleep: Callable[[float], None],
    *,
    attempts: int = 48,
    schema: int | None = None,
    role: str | None = None,
    before_schema: int | None = None,
) -> None:
    installation = plan["installation"]
    last = ""
    for attempt in range(attempts):
        live_schema = _live_schema(installation) if before_schema is not None else None
        if before_schema is not None:
            need(live_schema in (before_schema, plan["state_schema"]), "unexpected database schema")
        try:
            if before_schema is not None:
                need(live_schema == plan["state_schema"], "migration not yet complete")
            result = _json_command(
                runner, [_runtime_cli(runtime), "status", installation["hub_config"]]
            )
            health = result["runtime_health"]
            dep = health["deployment_revision"]
            need(
                result["schema_version"] == (plan["state_schema"] if schema is None else schema),
                "status schema mismatch",
            )
            need(
                dep["status"] == "converged" and dep["git_sha"] == runtime["git_sha"],
                "revision not converged",
            )
            components = [
                health[k] for k in ("controller", "monitor", "sender", "project_provisioner")
            ] + health["provider_workers"]
            components = [c for c in components if c["status"] != "not_configured"]
            need(len(components) == dep["required_components"], "component count mismatch")
            for component in components:
                need(
                    component["release_git_sha"] == runtime["git_sha"]
                    and component["release_clean"] is True
                    and component["status"] not in ("missing", "stale", "unknown"),
                    "component not fresh and clean",
                )
            for unit in plan["restart_order"]:
                # This loop is the retry: settling here would multiply its bound.
                _verify_unit(
                    unit, runtime["root"], plan["unit_runtime_modes"][unit], manager, settle=0
                )
            _verify_config_only(
                plan,
                manager,
                untouched,
                role or ("new_runtime" if runtime == plan["new_runtime"] else "old_runtime"),
                started=True,
            )
            for unit in plan["untouched_units"]:
                need(
                    manager.identity(unit) == untouched["units"][unit],
                    "untouched unit changed: " + unit,
                )
            manager.verify_effective(plan["untouched_units"][0])
            if untouched["units"][plan["untouched_units"][0]]["ActiveState"] == "active":
                need(
                    _socket(runner, runtime, installation) == untouched["socket"],
                    "codex socket changed",
                )
            return
        except (
            Refused,
            OSError,
            ValueError,
            KeyError,
            StopIteration,
            subprocess.SubprocessError,
        ) as exc:
            last = str(exc)
        if attempt + 1 < attempts:
            sleep(5)
    raise Refused("convergence failed: " + last)


def _effect(
    window: Path, name: str, operation: Callable[[], Any], fault: Callable[[str], None] | None
) -> Any:
    _journal(window, "before", effect=name)
    if fault:
        fault("before:" + name)
    try:
        result = operation()
    except Exception as exc:
        _journal(window, "effect_failed", effect=name, error=str(exc))
        raise
    if fault:
        fault("effect:" + name)
    _journal(window, "after", effect=name)
    if fault:
        fault("after:" + name)
    return result


def _sync_output(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _sync_dir(path.parent)


def _install_files(
    window: Path,
    plan_dir: Path,
    plan: dict[str, Any],
    role: str,
    fault: Callable[[str], None] | None,
    amended: dict[str, set[str]] | None = None,
) -> None:
    """Install the plan's ``role`` snapshots.

    ``amended`` maps a path to the acknowledged digests of dropped config
    amendments; such a file is restored to its snapshot as well.
    """
    amended = amended or {}
    _check_recovery_inventory(plan, amended)
    for entry in plan["files"]:
        path = Path(entry["path"])
        current = _file_bytes(path)
        digest = hashlib.sha256(current).hexdigest() if current is not None else None
        valid = {
            item["sha256"] if item else None
            for item in (
                entry["before"],
                entry["after"],
                *([entry["rollback"]] if "rollback" in entry else []),
            )
        } | amended.get(entry["path"], set())
        need(digest in valid, "unrelated file edit: " + str(path))
        snapshot = entry[role]
        mode = (
            entry["mode"]
            if role == "before"
            else entry.get("rollback_mode", entry["after_mode"])
            if role == "rollback"
            else entry["after_mode"]
        )
        dropped = entry["path"] in amended and digest != (snapshot["sha256"] if snapshot else None)
        if not entry["changed"] and "rollback" not in entry and not dropped:
            continue
        if (
            "rollback" in entry
            and digest == (snapshot["sha256"] if snapshot else None)
            and (current is None or stat.S_IMODE(path.stat().st_mode) == mode)
        ):
            continue
        data = _private_bytes(plan_dir / snapshot["copy"]) if snapshot else None
        _effect(
            window,
            role + ":" + str(path),
            lambda: _atomic_file(path, data, mode),
            fault,
        )


def _check_recovery_inventory(
    plan: dict[str, Any], amended: dict[str, set[str]] | None = None
) -> None:
    amended = amended or {}
    for entry in plan["files"]:
        path = Path(entry["path"])
        current = _file_bytes(path)
        digest = hashlib.sha256(current).hexdigest() if current is not None else None
        valid = {
            item["sha256"] if item else None
            for item in (
                entry["before"],
                entry["after"],
                *([entry["rollback"]] if "rollback" in entry else []),
            )
        }
        acknowledged = amended.get(entry["path"], set())
        need(digest in valid | acknowledged, "unrelated file edit: " + str(path))
        if current is not None:
            modes = set()
            for label, mode in (
                ("before", entry["mode"]),
                ("after", entry["after_mode"]),
                ("rollback", entry.get("rollback_mode", entry["mode"])),
            ):
                snapshot = entry.get(label)
                if snapshot and snapshot["sha256"] == digest:
                    modes.add(mode)
            if digest in acknowledged:
                # Amendments keep the installed mode, which they require to be after_mode.
                modes.add(entry["after_mode"])
            need(stat.S_IMODE(path.stat().st_mode) in modes, "unrelated file mode: " + str(path))


def _stop(
    window: Path, plan: dict[str, Any], manager: Any, fault: Callable[[str], None] | None
) -> None:
    installation = plan["installation"]
    for unit in [
        installation["monitor_timer"],
        installation["monitor_service"],
        *reversed(plan["restart_order"]),
    ]:
        _effect(window, "stop:" + unit, lambda unit=unit: manager.stop(unit), fault)
        if unit in plan["restart_order"]:
            need(
                manager.identity(unit)["ActiveState"] in ("inactive", "failed"),
                "unit failed to stop: " + unit,
            )


def _start(
    window: Path,
    plan: dict[str, Any],
    manager: Any,
    sleep: Callable[[float], None],
    fault: Callable[[str], None] | None,
) -> bool:
    installation = plan["installation"]
    for unit in plan["restart_order"]:
        _effect(window, "start:" + unit, lambda unit=unit: manager.start(unit), fault)
        need(manager.identity(unit)["ActiveState"] == "active", "unit failed to start: " + unit)
    monitor_ok = False
    for attempt in range(2):
        error = []

        def run_monitor() -> None:
            try:
                manager.start(installation["monitor_service"])
            except (Refused, OSError, subprocess.SubprocessError) as exc:
                error.append(str(exc))

        _effect(window, "monitor:" + str(attempt + 1), run_monitor, fault)
        if not error:
            monitor_ok = True
            break
        _journal(window, "monitor_failed", attempt=attempt + 1, error=error[0])
        if attempt == 0:
            _effect(window, "monitor-retry-wait", lambda: sleep(60), fault)
    _effect(
        window,
        "start:" + installation["monitor_timer"],
        lambda: manager.start(installation["monitor_timer"]),
        fault,
    )
    return monitor_ok


def _check_runtime(runner: Callable[..., Any], runtime: dict[str, Any], message: str) -> None:
    info = _json_command(runner, [_runtime_cli(runtime), "release-info"])
    need(
        info.get("git_sha") == runtime["git_sha"]
        and info.get("clean_tree") is True
        and (
            "package_version" not in runtime
            or info.get("package_version") == runtime["package_version"]
        ),
        message,
    )
    if runtime["tree_sha256"] is not None:
        _runtime_command(Path(runtime["root"]), "verify", runner, runtime["tree_sha256"])


def _preflight(
    plan: dict[str, Any],
    manager: Any,
    runner: Callable[..., Any],
    load: Callable[[], float],
    role: str,
) -> dict[str, Any]:
    """Check an idle installation running ``role``; return the untouched identities."""
    installation = plan["installation"]
    need(not _busy(installation), "provider jobs busy")
    need(load() < 4, "load too high")
    for unit in plan["restart_order"]:
        # Fail fast: waiting would let the busy and load checks above go stale.
        _verify_unit(
            unit,
            plan[role]["root"],
            plan["unit_runtime_modes"][unit],
            manager,
            settle=0,
        )
    # A timer has no MainPID, so its full identity is unavailable; only its state matters.
    need(
        manager.property(installation["monitor_timer"], "ActiveState") == "active",
        "monitor timer inactive",
    )
    manager.preflight_extra()
    identities = {
        unit: manager.identity(unit)
        for unit in set(plan["untouched_units"]) | set(plan["config_only_units"])
    }
    for unit in plan["config_only_units"]:
        need(
            bool(identities[unit]["ActiveState"]) and identities[unit]["MainPID"].isdecimal(),
            "invalid config-only identity: " + unit,
        )
    app = plan["untouched_units"][0]
    need(manager.enabled(app), "managed app-server is not enabled")
    need(
        all(identities[unit]["ActiveState"] == "active" for unit in plan["untouched_units"][1:]),
        "untouched unit inactive",
    )
    need(
        identities[plan["untouched_units"][0]]["ActiveState"] in ("active", "failed"),
        "invalid managed app-server state",
    )
    socket = _socket(runner, plan[role], installation)
    if identities[app]["ActiveState"] == "active":
        pinned = json.loads(Path(installation["codex_manifest"]).read_text())["codex_binary"][
            "path"
        ]
        need(
            manager.executable(app) == pinned and socket == "listening",
            "managed app-server pin or socket mismatch",
        )
    return {"units": identities, "socket": socket}


def _untouched_snapshot(plan: dict[str, Any], manager: Any) -> dict[str, Any]:
    """The current untouched and config-only identities, without health requirements.

    The socket state is added by the caller with the runtime that it will start.
    """
    identities = {
        unit: manager.identity(unit)
        for unit in set(plan["untouched_units"]) | set(plan["config_only_units"])
    }
    return {"units": identities, "socket": None}


def _with_socket(
    baseline: dict[str, Any], plan: dict[str, Any], runner: Callable[..., Any], role: str
) -> dict[str, Any]:
    app = plan["untouched_units"][0]
    if baseline["units"][app]["ActiveState"] != "active":
        return baseline
    return {**baseline, "socket": _socket(runner, plan[role], plan["installation"])}


def _create_manifest(
    runner: Callable[..., Any], plan: dict[str, Any], manifest: Path, backup: Path
) -> None:
    # The artifact of the runtime that recovery restores at the target schema.
    rollback_wheel = plan["rollback_wheel" if "rollback_runtime" in plan else "old_wheel"]
    runner(
        [
            _runtime_cli(plan["new_runtime"]),
            "release-manifest",
            "create",
            str(manifest),
            "--active-artifact",
            plan["new_wheel"],
            "--rollback-artifact",
            rollback_wheel,
            "--config",
            plan["installation"]["hub_config"],
            "--backup",
            str(backup),
        ]
    )
    _sync_output(manifest)


def _recover(
    window: Path,
    metadata: dict[str, Any],
    manager: Any,
    runner: Callable[..., Any],
    sleep: Callable[[float], None],
    fault: Callable[[str], None] | None = None,
    amended: dict[str, set[str]] | None = None,
    untouched: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Recover the window; ``untouched`` replaces its saved baseline for an explicit rollback."""
    try:
        plan_dir = Path(metadata["plan_dir"])
        plan = verify_saved_plan(plan_dir, check_live=False)
        need(
            json.loads(_private_bytes(plan_dir / "plan.json"))["sha256"] == metadata["plan_sha256"],
            "window plan changed",
        )
        _check_recovery_inventory(plan, amended)
        _check_window_dependencies(plan, manager)
        _journal(window, "rollback_started")
        _stop(window, plan, manager, fault)
        _check_recovery_inventory(plan, amended)
        live = _live_schema(plan["installation"])
        before = plan.get("state_schema_before", plan["state_schema"])
        if live == before:
            role, file_role, artifact = "old_runtime", "before", "old"
        elif live == plan["state_schema"] and "rollback_runtime" in plan:
            role, file_role, artifact = "rollback_runtime", "rollback", "rollback"
        else:
            raise Refused("unexpected database schema; owner decision required; no backup restored")
        _journal(window, "rollback_target", schema=live, target=role, git_sha=plan[role]["git_sha"])
        need(
            hashlib.sha256(Path(plan[artifact + "_wheel"]).read_bytes()).hexdigest()
            == plan[artifact + "_wheel_sha256"],
            "rollback artifact changed",
        )
        runtime = plan[role]
        _check_runtime(runner, runtime, "rollback runtime identity changed")
        _install_files(window, plan_dir, plan, file_role, fault, amended)
        baseline = metadata["untouched"]
        if untouched is not None:
            # Observe the socket with the runtime that recovery starts and the config
            # it restored, never with the release that may be the reason for this
            # rollback; untouched units are never stopped, so their state still holds.
            baseline = _with_socket(untouched, plan, runner, role)
            _journal(window, "recovery_baseline", untouched=baseline)
        _effect(window, "daemon-reload", manager.reload, fault)
        _check_window_dependencies(plan, manager)
        _verify_config_only(plan, manager, baseline, role)
        _start(window, plan, manager, sleep, fault)
        _verify(runtime, plan, manager, runner, baseline, sleep, schema=live, role=role)
        outcome = "ROLLED_BACK"
    except Exception as exc:
        _journal(window, "recovery_error", error=str(exc))
        _journal(window, "RECOVERY_REQUIRED")
        return {"id": window.name, "outcome": "RECOVERY_REQUIRED", "error": str(exc)}
    _journal(window, outcome)
    return {"id": window.name, "outcome": outcome}


def apply_window(
    installation: dict[str, Any],
    plan_dir: Path,
    manager: Any,
    runner: Callable[..., Any],
    sleep: Callable[[float], None],
    *,
    load: Callable[[], float] = lambda: os.getloadavg()[0],
    fault: Callable[[str], None] | None = None,
    expected_digest: str | None = None,
    input_path: str | None = None,
) -> dict[str, Any]:
    with _lock(installation):
        _unfinished(installation, input_path)
        need(
            plan_dir.is_absolute()
            and plan_dir.parent == Path(installation["ops_dir"])
            and plan_dir.name.startswith("plan-"),
            "plan must be in the ops directory",
        )
        plan = verify_saved_plan(plan_dir)
        _check_store_pairs(
            installation, plan_dir, plan, ("new_runtime", "old_runtime", "rollback_runtime")
        )
        missing_rollback = "schema-59 plan requires a bound rollback artifact"
        same_schema = plan.get("state_schema_before", plan["state_schema"]) == plan["state_schema"]
        if same_schema:
            need(not any(key.startswith("rollback_") for key in plan), SAME_SCHEMA_ROLLBACK)
        else:
            need(
                "rollback_runtime" in plan and isinstance(plan.get("rollback_wheel"), str),
                "forward migration plan requires a bound rollback artifact",
            )
        # A schema-59 same-schema plan must state both schemas and the previous
        # runtime's planned capability; that runtime is rechecked below.
        previous_rollback = plan["state_schema"] >= 59 and same_schema
        if previous_rollback:
            need(
                plan.get("state_schema_before") == plan["state_schema"]
                and type(plan["old_runtime"].get(DECLARATION)) is bool,
                missing_rollback,
            )
        need(
            expected_digest is None
            or json.loads(_private_bytes(plan_dir / "plan.json"))["sha256"] == expected_digest,
            "plan digest changed",
        )
        for label in ("old", "new", *(["rollback"] if "rollback_runtime" in plan else [])):
            need(
                hashlib.sha256(Path(plan[label + "_wheel"]).read_bytes()).hexdigest()
                == plan[label + "_wheel_sha256"],
                "release artifact changed",
            )
        need(plan["installation"] == installation, "installation changed since planning")
        for label in (
            "old_runtime",
            "new_runtime",
            *(["rollback_runtime"] if "rollback_runtime" in plan else []),
        ):
            runtime = plan[label]
            info = _json_command(runner, [_runtime_cli(runtime), "release-info"])
            need(
                info.get("git_sha") == runtime["git_sha"]
                and info.get("clean_tree") is True
                and (
                    "package_version" not in runtime
                    or info.get("package_version") == runtime["package_version"]
                ),
                "runtime identity changed",
            )
            if label == "old_runtime" and previous_rollback:
                need(
                    type(info.get(DECLARATION)) is bool
                    and info[DECLARATION] == runtime[DECLARATION],
                    "previous runtime declaration capability changed",
                )
            if runtime["tree_sha256"] is not None:
                _runtime_command(Path(runtime["root"]), "verify", runner, runtime["tree_sha256"])
        _validate_plan_configs(plan_dir, plan, "new_runtime", "after", "candidate_config", runner)
        if "rollback_runtime" in plan:
            _validate_plan_configs(
                plan_dir, plan, "rollback_runtime", "rollback", "rollback_config", runner
            )
        with closing(
            sqlite3.connect(Path(installation["state_db"]).as_uri() + "?mode=ro", uri=True)
        ) as db:
            need(
                db.execute("PRAGMA user_version").fetchone()[0]
                == plan.get("state_schema_before", plan["state_schema"]),
                "schema changed",
            )
        untouched = _preflight(plan, manager, runner, load, "old_runtime")
        verify_saved_plan(plan_dir)
        _check_window_dependencies(plan, manager)
        window = Path(tempfile.mkdtemp(prefix=".window-preparing-", dir=installation["ops_dir"]))
        metadata = {
            "plan_dir": str(plan_dir),
            "plan_sha256": json.loads(_private_bytes(plan_dir / "plan.json"))["sha256"],
            "untouched": untouched,
        }
        _atomic_file(window / "metadata.json", canonical_document(metadata), 0o600)
        _journal(window, "started")
        previous = last_window(installation)
        sequence = time.time_ns()
        if previous is not None:
            match = re.fullmatch(r"window-([0-9]{20})-[a-z0-9_]+", previous["id"])
            if match is None:
                raise Refused("invalid previous window ID")
            sequence = max(sequence, int(match.group(1)) + 1)
        need(sequence < 10**20, "window sequence exhausted")
        final = window.with_name(
            "window-" + f"{sequence:020d}" + "-" + window.name.removeprefix(".window-preparing-")
        )
        window.rename(final)
        window = final
        _sync_dir(window.parent)
        try:
            _stop(window, plan, manager, fault)
            need(not _busy(installation), "provider jobs appeared while stopping")
            verify_saved_plan(plan_dir)
            backup = window / "state.db"
            manifest = window / MANIFEST

            def backup_state() -> None:
                runner(
                    [
                        _runtime_cli(plan["old_runtime"]),
                        "backup",
                        installation["state_db"],
                        str(backup),
                    ]
                )
                _sync_output(backup)

            need(
                _live_schema(installation) == plan.get("state_schema_before", plan["state_schema"]),
                "schema changed under quiescence",
            )
            _effect(window, "backup", backup_state, fault)
            _effect(
                window,
                "manifest-create",
                lambda: _create_manifest(runner, plan, manifest, backup),
                fault,
            )
            checked = hashlib.sha256(_private_bytes(manifest)).hexdigest()
            _effect(
                window,
                "manifest-verify",
                lambda: runner(
                    [
                        _runtime_cli(plan["new_runtime"]),
                        "release-manifest",
                        "verify",
                        str(manifest),
                    ]
                    + (
                        ["--state", installation["state_db"]]
                        if plan.get("state_schema_before", plan["state_schema"])
                        == plan["state_schema"]
                        else []
                    )
                ),
                fault,
            )
            # Amendments compare their reference against this digest, which must be
            # that of the bytes read before and after verification.
            need(
                hashlib.sha256(_private_bytes(manifest)).hexdigest() == checked,
                "release manifest changed during verification",
            )
            _journal(window, "manifest_bound", sha256=checked)
            verify_saved_plan(plan_dir)
            _check_window_dependencies(plan, manager)
            _install_files(window, plan_dir, plan, "after", fault)
            _effect(window, "daemon-reload", manager.reload, fault)
            _check_window_dependencies(plan, manager)
            _verify_config_only(plan, manager, untouched, "new_runtime")
            need(
                _live_schema(installation) == plan.get("state_schema_before", plan["state_schema"]),
                "schema changed before new runtime start",
            )
            monitor_ok = _start(window, plan, manager, sleep, fault)
            _verify(
                plan["new_runtime"],
                plan,
                manager,
                runner,
                untouched,
                sleep,
                before_schema=plan.get("state_schema_before", plan["state_schema"]),
            )
            outcome = "ACCEPTED" if monitor_ok else "CORE_HEALTHY_MONITOR_INCOMPLETE"
            _journal(window, outcome)
            return {"id": window.name, "outcome": outcome}
        except Exception as exc:
            _journal(window, "apply_failed", error=str(exc))
            return _recover(window, metadata, manager, runner, sleep)


def _check_store_pairs(
    installation: dict[str, Any], plan_dir: Path, plan: dict[str, Any], roles: tuple[str, ...]
) -> None:
    """Every release a window may run on the store needs evidence for the active CLI.

    Runs under the deploy lock right before a switch, so a promotion after the
    plan cannot leave an ungated pair (ADR 0113). Releases with an embedded CLI
    are skipped; they never run the store.
    """
    if not store_mode(installation):
        return
    store = Path(installation["claude_store"])
    stable = str(claude_store.stable_path(store))
    executables: dict[str, set[object]] = {}
    for entry in plan["files"]:
        if entry["kind"] != "claude_config":
            continue
        for label, role in (("before", "old_runtime"), ("after", "new_runtime")):
            if entry.get(label) is not None:
                config = json.loads(_private_bytes(plan_dir / entry[label]["copy"]))
                executables.setdefault(role, set()).update(
                    agent.get("executable")
                    for agent in config.get("agents", [])
                    if isinstance(agent, dict) and agent.get("runtime") == "claude"
                )
    executables.setdefault("rollback_runtime", executables.get("new_runtime", set()))
    try:
        claude_store.check_store(store)
        for role in roles:
            if role in plan and stable in executables.get(role, set()):
                runtime = plan[role]
                claude_store.ensure_pair(
                    store,
                    claude_store.release_gate(Path(runtime["root"])),
                    runtime["git_sha"],
                    now=time.time(),
                )
    except claude_store.StoreRefused as exc:
        raise Refused(str(exc)) from None
    except claude_store.NeedsInspection as exc:
        raise Refused("Claude CLI store needs inspection: " + str(exc)) from None


def rollback_window(
    installation: dict[str, Any],
    window_id: str,
    manager: Any,
    runner: Callable[..., Any],
    sleep: Callable[[float], None],
    *,
    drop_amendments: bool = False,
) -> dict[str, Any]:
    need(re.fullmatch(r"window-[0-9]{20}-[a-z0-9_]+", window_id) is not None, "invalid window ID")
    with _lock(installation):
        window = Path(installation["ops_dir"]) / window_id
        need(window.is_dir() and not window.is_symlink(), "window not found")
        metadata = json.loads(_private_bytes(window / "metadata.json"))
        plan = verify_saved_plan(Path(metadata["plan_dir"]), check_live=False)
        need(plan["installation"] == installation, "installation changed")
        previous = last_window(installation)
        need(previous is not None and previous["id"] == window_id, "superseded window")
        assert previous is not None
        _check_store_pairs(installation, Path(metadata["plan_dir"]), plan, ("old_runtime",))
        # Owner decision 2026-11-23: a release rollback restores the pre-deploy
        # Hub config; amendments made after the window are dropped and listed,
        # and the caller acknowledges that with --drop-amendments. A rolled-back
        # window has already dropped them.
        dropped, amended = [], None
        if previous["state"] != "ROLLED_BACK":
            head, chain = _amendment_chain(window, plan)
            dropped = [
                {"id": item.name, "state": state, "changed_keys": record["changed_keys"]}
                for item, record, state in chain
                if state != "REVERTED"
            ]
            # Only the installed head, or an unfinished amendment's two states.
            allowed = {head}
            if chain and chain[-1][2] not in AMENDMENT_DONE:
                allowed |= {chain[-1][1]["before"], chain[-1][1]["after"]}
            if dropped:
                amended = {installation["hub_config"]: allowed}
        need(
            drop_amendments or not dropped,
            "rollback restores the pre-deploy Hub config and drops its amendments "
            + ", ".join(item["id"] for item in dropped)
            + "; pass --drop-amendments",
        )
        need(not drop_amendments or dropped, "no Hub config amendments to drop")
        _check_recovery_inventory(plan, amended)
        # An explicit recovery may follow a reboot: untouched units then have new
        # identities, and this recovery must only leave them as it finds them.
        baseline = _untouched_snapshot(plan, manager)
        if dropped:
            _journal(window, "amendments_drop_acknowledged", amendments=[d["id"] for d in dropped])
        result = _recover(
            window, metadata, manager, runner, sleep, amended=amended, untouched=baseline
        )
        if dropped and result["outcome"] == "ROLLED_BACK":
            result["dropped_amendments"] = dropped
        return result


def _amendments(window: Path) -> list[Path]:
    """The window's Hub config amendments in order; one in preparation is not listed."""
    paths = sorted(window.glob("amendment-*"), key=lambda path: path.name)
    for path in paths:
        need(
            re.fullmatch(r"amendment-[0-9]{3}", path.name) is not None
            and path.is_dir()
            and not path.is_symlink(),
            "invalid amendment: " + path.name,
        )
    return paths


def _amendment_state(path: Path) -> str:
    rows = _journal_rows(path / "journal.jsonl")
    need(bool(rows), "empty amendment journal")
    return rows[-1]["phase"]


_AMENDMENT_KEYS = {
    "window",
    "config",
    "mode",
    "before",
    "after",
    "changed_keys",
    "adopted",
    "untouched",
}


def _amendment_metadata(path: Path) -> dict[str, Any]:
    record = json.loads(_private_bytes(path / "metadata.json"))
    need(
        isinstance(record, dict)
        and set(record) == _AMENDMENT_KEYS
        and record["window"] == path.parent.name,
        "invalid amendment metadata: " + path.name,
    )
    return record


def _amendment_bytes(path: Path, record: dict[str, Any], label: str) -> bytes:
    data = _private_bytes(path / ("config-" + label))
    need(hashlib.sha256(data).hexdigest() == record[label], "amendment copy changed: " + path.name)
    return data


def _hub_entry(plan: dict[str, Any]) -> dict[str, Any]:
    path = plan["installation"]["hub_config"]
    entries = [e for e in plan["files"] if e["kind"] == "hub_config" and e["path"] == path]
    need(len(entries) == 1 and entries[0]["after"] is not None, "missing Hub config snapshot")
    return entries[0]


def _amendment_chain(
    window: Path, plan: dict[str, Any]
) -> tuple[str, list[tuple[Path, dict[str, Any], str]]]:
    """The Hub config digest the window and its finished amendments installed, and the chain.

    Only the newest amendment may be unfinished; each starts from the head before it.
    """
    head = _hub_entry(plan)["after"]["sha256"]
    items = _amendments(window)
    chain = []
    for index, item in enumerate(items):
        record = _amendment_metadata(item)
        state = _amendment_state(item)
        need(
            record["config"] == plan["installation"]["hub_config"] and record["before"] == head,
            "broken amendment chain: " + item.name,
        )
        need(
            state in AMENDMENT_DONE or index == len(items) - 1,
            "unfinished config amendment: " + item.name,
        )
        if state in AMENDMENT_EFFECTIVE:
            head = record["after"]
        chain.append((item, record, state))
    return head, chain


def _config_object(data: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        need(len({key for key, _ in pairs}) == len(pairs), "duplicate Hub config key")
        return dict(pairs)

    def constant(name: str) -> None:
        raise Refused("non-JSON number in Hub config: " + name)

    # Decimal keeps every digit, so no textual change can hide behind a float.
    value = json.loads(
        data, object_pairs_hook=unique, parse_float=decimal.Decimal, parse_constant=constant
    )
    need(isinstance(value, dict), "Hub config must be an object")
    return value


def _same(left: object, right: object) -> bool:
    """JSON equality that keeps types apart: ``1``, ``1.0`` and ``true`` all differ."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        assert isinstance(right, dict)
        return left.keys() == right.keys() and all(_same(left[k], right[k]) for k in left)
    if isinstance(left, list):
        assert isinstance(right, list)
        return len(left) == len(right) and all(map(_same, left, right))
    return left == right


def config_changes(before: bytes, after: bytes) -> list[str]:
    """The changed top-level keys; refuse any key outside ``RECONFIGURABLE_KEYS``."""
    old, new = _config_object(before), _config_object(after)
    changed = sorted(
        key
        for key in old.keys() | new.keys()
        if key not in old or key not in new or not _same(old[key], new[key])
    )
    need(bool(changed), "no Hub config change")
    refused = [key for key in changed if key not in RECONFIGURABLE_KEYS]
    need(not refused, "Hub config keys are not reconfigurable: " + ", ".join(refused))
    return changed


def _manifest_document(data: bytes, path: Path) -> dict[str, Any]:
    document = json.loads(data)
    need(
        isinstance(document, dict)
        and isinstance(document.get("configuration"), dict)
        and isinstance(document.get("state_backup"), dict),
        "invalid release manifest: " + str(path),
    )
    return document


def _bindings(document: dict[str, Any]) -> dict[str, Any]:
    """Everything a manifest binds except the config and its creation time."""
    return {k: v for k, v in document.items() if k not in ("created_at", "configuration")}


def _accepted_window(installation: dict[str, Any]) -> tuple[Path, Path, dict[str, Any]]:
    """The newest window, which must be accepted, its plan directory and plan."""
    last = last_window(installation)
    need(last is not None and last["state"] == "ACCEPTED", "the newest window is not accepted")
    assert last is not None
    window = Path(installation["ops_dir"]) / last["id"]
    metadata = json.loads(_private_bytes(window / "metadata.json"))
    plan_dir = Path(metadata["plan_dir"])
    plan = verify_saved_plan(plan_dir, check_live=False)
    need(
        json.loads(_private_bytes(plan_dir / "plan.json"))["sha256"] == metadata["plan_sha256"],
        "window plan changed",
    )
    need(plan["installation"] == installation, "installation changed")
    return window, plan_dir, plan


def _config_head(window: Path, plan_dir: Path, plan: dict[str, Any]) -> bytes:
    """The Hub config bytes the window and its finished amendments installed."""
    head, chain = _amendment_chain(window, plan)
    need(all(state in AMENDMENT_DONE for _, _, state in chain), "unfinished config amendment")
    effective = [(item, record) for item, record, state in chain if state in AMENDMENT_EFFECTIVE]
    if effective:
        data = _amendment_bytes(*effective[-1], "after")
    else:
        data = _private_bytes(plan_dir / _hub_entry(plan)["after"]["copy"])
    need(hashlib.sha256(data).hexdigest() == head, "Hub config snapshot changed")
    return data


def _check_accepted_inventory(plan: dict[str, Any]) -> None:
    """Every other window file still has the bytes and mode the window installed."""
    for entry in plan["files"]:
        if entry["path"] == plan["installation"]["hub_config"]:
            continue
        path = Path(entry["path"])
        current = _file_bytes(path)
        after = entry["after"]
        need(
            (current is None and after is None)
            or (
                current is not None
                and after is not None
                and hashlib.sha256(current).hexdigest() == after["sha256"]
                and stat.S_IMODE(path.stat().st_mode) == entry["after_mode"]
            ),
            "installed file differs from the window: " + str(path),
        )


def reconfigure(
    installation: dict[str, Any],
    manager: Any,
    runner: Callable[..., Any],
    sleep: Callable[[float], None],
    *,
    candidate: Path | None,
    load: Callable[[], float] = lambda: os.getloadavg()[0],
    fault: Callable[[str], None] | None = None,
    input_path: str | None = None,
) -> dict[str, Any]:
    """Amend the accepted window's Hub config, or adopt an edit when ``candidate`` is None."""
    with _lock(installation):
        _unfinished(installation, input_path)
        window, plan_dir, plan = _accepted_window(installation)
        for abandoned in window.glob(".amendment-preparing-*"):
            # A process loss before publication left private copies and no effect.
            shutil.rmtree(abandoned)
        need(_live_schema(installation) == plan["state_schema"], "schema changed since the window")
        head = _config_head(window, plan_dir, plan)
        hub = Path(installation["hub_config"])
        current = _file_bytes(hub)
        need(current is not None, "missing Hub config")
        assert current is not None
        mode = stat.S_IMODE(hub.stat().st_mode)
        need(mode == _hub_entry(plan)["after_mode"], "Hub config mode changed")
        if candidate is None:
            need(current != head, "no Hub config edit to adopt")
            before, after = head, current
        else:
            need(
                current == head,
                "Hub config was edited outside reconfigure; record it with --adopt first",
            )
            need(candidate.is_absolute(), "candidate config path must be absolute")
            before, after = current, _private_bytes(candidate)
        changed = config_changes(before, after)
        rollback = "rollback" if "rollback_runtime" in plan else "old"
        for label in ("new", rollback):
            need(
                hashlib.sha256(Path(plan[label + "_wheel"]).read_bytes()).hexdigest()
                == plan[label + "_wheel_sha256"],
                "release artifact changed",
            )
        # The new manifest must bind the window's backup, so compare it first. A
        # window from before this record has no digest; its manifest is trusted as found.
        reference_bytes = _private_bytes(window / MANIFEST)
        bound = [
            row["sha256"]
            for row in _journal_rows(window / "journal.jsonl")
            if row["phase"] == "manifest_bound"
        ]
        need(
            len(bound) <= 1
            and all(hashlib.sha256(reference_bytes).hexdigest() == item for item in bound),
            "window manifest differs from the one the window verified",
        )
        reference = _manifest_document(reference_bytes, window / MANIFEST)
        backup = window / "state.db"
        need(
            reference["state_backup"].get("path") == str(backup)
            and backup.is_file()
            and not backup.is_symlink()
            and hashlib.sha256(backup.read_bytes()).hexdigest()
            == reference["state_backup"].get("sha256"),
            "window state backup differs from the window manifest",
        )
        _check_accepted_inventory(plan)
        _check_runtime(runner, plan["new_runtime"], "runtime identity changed")
        staging = Path(tempfile.mkdtemp(prefix=".amendment-preparing-", dir=window))
        try:
            _write_private(staging / "config-before", before)
            _write_private(staging / "config-after", after)
            result = _json_command(
                runner,
                [_runtime_cli(plan["new_runtime"]), "validate-hub", str(staging / "config-after")],
            )
            need(result.get("ok") is True, "candidate Hub config validation failed")
            _check_window_dependencies(plan, manager)
            untouched = _preflight(plan, manager, runner, load, "new_runtime")
            _verify_config_only(plan, manager, untouched, "new_runtime")
            record = {
                "window": window.name,
                "config": installation["hub_config"],
                "mode": mode,
                "before": hashlib.sha256(before).hexdigest(),
                "after": hashlib.sha256(after).hexdigest(),
                "changed_keys": changed,
                "adopted": candidate is None,
                "untouched": untouched,
            }
            _atomic_file(staging / "metadata.json", canonical_document(record), 0o600)
            _journal(staging, "started")
            index = len(_amendments(window)) + 1
            need(index < 1000, "amendment sequence exhausted")
        except BaseException:
            shutil.rmtree(staging)
            raise
        path = window / f"amendment-{index:03d}"
        staging.rename(path)
        _sync_dir(window)
        return _apply_amendment(
            window, path, plan, record, reference, manager, runner, sleep, fault
        )


def recover_amendment(
    installation: dict[str, Any],
    manager: Any,
    runner: Callable[..., Any],
    sleep: Callable[[float], None],
    fault: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Restore the Hub config an interrupted amendment found and restart on it."""
    with _lock(installation):
        window, _, plan = _accepted_window(installation)
        _, chain = _amendment_chain(window, plan)
        need(bool(chain) and chain[-1][2] not in AMENDMENT_DONE, "no unfinished config amendment")
        path, record, _ = chain[-1]
        _check_runtime(runner, plan["new_runtime"], "runtime identity changed")
        # The services may be running again, for example after a reboot.
        need(not _busy(installation), "provider jobs busy; recover when idle")
        baseline = _with_socket(_untouched_snapshot(plan, manager), plan, runner, "new_runtime")
        return _revert_amendment(
            window, path, plan, record, manager, runner, sleep, fault, untouched=baseline
        )


def _write_config(
    path: Path, record: dict[str, Any], label: str, fault: Callable[[str], None] | None
) -> None:
    hub = Path(record["config"])
    data = _amendment_bytes(path, record, label)
    current = _file_bytes(hub)
    digest = hashlib.sha256(current).hexdigest() if current is not None else None
    need(digest in (record["before"], record["after"]), "unrelated Hub config edit")
    need(stat.S_IMODE(hub.stat().st_mode) == record["mode"], "unrelated Hub config mode")
    if digest != record[label]:
        _effect(path, "config:" + label, lambda: _atomic_file(hub, data, record["mode"]), fault)


def _apply_amendment(
    window: Path,
    path: Path,
    plan: dict[str, Any],
    record: dict[str, Any],
    reference: dict[str, Any],
    manager: Any,
    runner: Callable[..., Any],
    sleep: Callable[[float], None],
    fault: Callable[[str], None] | None,
) -> dict[str, Any]:
    installation = plan["installation"]
    manifest = path / MANIFEST
    try:
        _stop(path, plan, manager, fault)
        need(not _busy(installation), "provider jobs appeared while stopping")
        _write_config(path, record, "after", fault)
        _effect(
            path,
            "manifest-create",
            lambda: _create_manifest(runner, plan, manifest, window / "state.db"),
            fault,
        )
        # Same wheels, schema and window backup; only the bound config differs.
        # The reported digest is the one of these checked bytes.
        checked = _private_bytes(manifest)
        document = _manifest_document(checked, manifest)
        need(
            _bindings(document) == _bindings(reference)
            and document["configuration"]
            == {"path": installation["hub_config"], "sha256": record["after"]},
            "new release manifest binds more than the config change",
        )
        manifest_sha256 = hashlib.sha256(checked).hexdigest()
        _effect(
            path,
            "manifest-verify",
            lambda: runner(
                [
                    _runtime_cli(plan["new_runtime"]),
                    "release-manifest",
                    "verify",
                    str(manifest),
                    "--state",
                    installation["state_db"],
                ]
            ),
            fault,
        )
        _check_window_dependencies(plan, manager)
        monitor_ok = _start(path, plan, manager, sleep, fault)
        _verify(
            plan["new_runtime"],
            plan,
            manager,
            runner,
            record["untouched"],
            sleep,
            role="new_runtime",
        )
        outcome = "ACCEPTED" if monitor_ok else "CORE_HEALTHY_MONITOR_INCOMPLETE"
        need(
            hashlib.sha256(_private_bytes(manifest)).hexdigest() == manifest_sha256,
            "new release manifest changed after its check",
        )
    except Exception as exc:
        _journal(path, "amendment_failed", error=str(exc))
        return _revert_amendment(window, path, plan, record, manager, runner, sleep, fault)
    _journal(path, outcome, manifest_sha256=manifest_sha256)
    return {
        "window": window.name,
        "amendment": path.name,
        "outcome": outcome,
        "changed_keys": record["changed_keys"],
        "adopted": record["adopted"],
        "config_sha256": record["after"],
        "manifest": str(manifest),
        "manifest_sha256": manifest_sha256,
    }


def _revert_amendment(
    window: Path,
    path: Path,
    plan: dict[str, Any],
    record: dict[str, Any],
    manager: Any,
    runner: Callable[..., Any],
    sleep: Callable[[float], None],
    fault: Callable[[str], None] | None = None,
    *,
    untouched: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Restore the amendment's previous bytes; ``untouched`` is a fresh recovery baseline."""
    result = {"window": window.name, "amendment": path.name, "changed_keys": record["changed_keys"]}
    try:
        _check_window_dependencies(plan, manager)
        _journal(path, "revert_started")
        if untouched is not None:
            _journal(path, "recovery_baseline", untouched=untouched)
        _stop(path, plan, manager, fault)
        _write_config(path, record, "before", fault)
        monitor_ok = _start(path, plan, manager, sleep, fault)
        _verify(
            plan["new_runtime"],
            plan,
            manager,
            runner,
            untouched or record["untouched"],
            sleep,
            role="new_runtime",
        )
        # A stale monitor record must not stand in for a fresh monitor run.
        need(monitor_ok, "monitor check failed after restoring the Hub config")
    except Exception as exc:
        _journal(path, "recovery_error", error=str(exc))
        _journal(path, "RECOVERY_REQUIRED")
        return {**result, "outcome": "RECOVERY_REQUIRED", "error": str(exc)}
    _journal(path, "REVERTED")
    return {**result, "outcome": "REVERTED"}


CLAUDE_ACTIONS = ("claude-status", "claude-promote", "claude-rollback")


def claude_action(
    installation: dict[str, Any],
    action: str,
    active: dict[str, Any],
    *,
    candidate: Path | None = None,
    candidate_version: str | None = None,
    now: Callable[[], float] = time.time,
    fetch: Callable[[str], bytes] = claude_store.fetch_url,
) -> dict[str, Any]:
    """One store operation for the active release; the caller holds the deploy lock."""
    store = Path(installation["claude_store"])
    if action == "claude-status":
        return claude_store.status(store, hub_git_sha=active["git_sha"], now=now())
    claude_store.open_store(store)
    root = Path(active["root"])
    need(
        active["claude_executable"] == str(claude_store.stable_path(store)),
        "active release does not run the Claude CLI store",
    )
    gate = claude_store.release_gate(root)
    check = claude_store.release_post_check(root)
    if action == "claude-rollback":
        need(candidate is None and candidate_version is None, "claude-rollback takes no candidate")
        return claude_store.rollback(
            store, hub_git_sha=active["git_sha"], gate=gate, post_check=check, now=now()
        )
    # Standing authorization covers only the nightly window (ADR 0113); no bypass.
    window = installation["claude_window"]
    need(claude_store.in_window(window, now()), "outside the nightly Claude promotion window")
    admitted = None
    if candidate is not None:
        version = candidate_version or candidate.resolve().name
        admitted = claude_store.admit(store, candidate, version, now=now(), fetch=fetch)
    else:
        need(candidate_version is None, "--candidate-version requires --candidate")
    result = claude_store.promote(
        store,
        hub_git_sha=active["git_sha"],
        gate=gate,
        post_check=check,
        now=now,
        window_open=lambda: claude_store.in_window(window, now()),
    )
    if admitted is not None:
        result["admitted"] = {
            key: admitted[key] for key in ("version", "sha256", "admitted_at", "source")
        }
    return result


def claude_main(
    args: argparse.Namespace,
    runner: Callable[..., Any],
    inspect_process: Callable[[str, str], bool] | None,
    runtime_owner: Callable[[Path], int],
    manager: Any,
) -> int:
    """Exit codes for the host update tool: 0 ok, 1 failed, 2 needs inspection, 3 refused."""
    result: dict[str, Any]
    store: Path | None = None
    hub_git_sha = None
    try:
        installation = load_installation(Path(args.installation))
        need(store_mode(installation), "installation does not use the Claude CLI store")
        need(
            not any((args.release, args.rollback_release, args.window, args.plan, args.config))
            and not (args.adopt or args.recover or args.drop_amendments),
            "unsupported option for " + args.action,
        )
        need(
            args.action == "claude-promote" or not (args.candidate or args.candidate_version),
            "--candidate and --candidate-version belong to claude-promote",
        )
        service = manager or ServiceManager(runner)
        inspector = inspect_process or (lambda unit, root: inspect_unit(unit, root, runner))
        with _lock(installation):
            if args.action != "claude-status":
                _unfinished(installation, args.installation)
            active = discover_installation(
                installation, runner, inspector, runtime_owner, manager=service
            )
            hub_git_sha = active["git_sha"]
            if args.action != "claude-status":
                store = Path(installation["claude_store"])
            result = claude_action(
                installation,
                args.action,
                active,
                candidate=None if args.candidate is None else Path(args.candidate),
                candidate_version=args.candidate_version,
            )
    except claude_store.NeedsInspection as exc:
        result = {"component": "claude", "outcome": "needs_inspection", "reason": str(exc)}
    except (
        Refused,
        claude_store.StoreRefused,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        result = {"component": "claude", "outcome": "refused", "reason": str(exc)}
    result["hub_git_sha"] = hub_git_sha
    if store is not None:
        try:
            # Only through a store that validates now; never through a refused one.
            claude_store.check_store(store)
            need((store / "store.json").exists(), "store is not initialized")
            result["receipt"] = str(claude_store.save_result(store, result, now=time.time()))
        except (OSError, Refused, claude_store.StoreRefused) as exc:
            # The outcome above is durable; a missing receipt never changes it.
            result["receipt_error"] = type(exc).__name__
    print(json.dumps(result, sort_keys=True))
    return claude_store.EXIT[result["outcome"]]


def main(
    argv: list[str] | None = None,
    *,
    runner: Callable[..., Any] = run_command,
    inspect_process: Callable[[str, str], bool] | None = None,
    runtime_owner: Callable[[Path], int] = lambda path: path.stat().st_uid,
    python: Path = Path("/usr/bin/python3.12"),
    manager: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    load: Callable[[], float] = lambda: os.getloadavg()[0],
) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "plan",
            "apply",
            "deploy",
            "rollback",
            "status",
            "reconfigure",
            *CLAUDE_ACTIONS,
        ),
    )
    parser.add_argument("--installation", required=True)
    parser.add_argument("--release")
    parser.add_argument("--rollback-release")
    parser.add_argument("--window")
    parser.add_argument("--plan")
    parser.add_argument("--config")
    parser.add_argument("--adopt", action="store_true")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--drop-amendments", action="store_true")
    parser.add_argument("--candidate")
    parser.add_argument("--candidate-version")
    args = parser.parse_args(argv)
    if args.action in CLAUDE_ACTIONS:
        return claude_main(args, runner, inspect_process, runtime_owner, manager)
    try:
        installation = load_installation(Path(args.installation))
        service = manager or ServiceManager(runner)
        release_options = args.release or args.rollback_release or args.window or args.plan
        amendment_options = [args.config is not None, args.adopt, args.recover]
        need(
            args.action == "reconfigure" or not any(amendment_options),
            "--config, --adopt and --recover belong to reconfigure",
        )
        need(
            args.action == "rollback" or not args.drop_amendments,
            "--drop-amendments belongs to rollback",
        )
        if args.action == "status":
            need(not release_options, "status takes no other options")
            print(json.dumps(window_status(installation)))
            return 0
        if args.action == "reconfigure":
            need(
                not release_options and amendment_options.count(True) == 1,
                "reconfigure requires exactly one of --config, --adopt or --recover",
            )
            if args.recover:
                result = recover_amendment(installation, service, runner, sleep)
            else:
                result = reconfigure(
                    installation,
                    service,
                    runner,
                    sleep,
                    candidate=None if args.config is None else Path(args.config),
                    load=load,
                    input_path=args.installation,
                )
        elif args.action == "rollback":
            need(
                args.window and not (args.release or args.rollback_release or args.plan),
                "rollback requires --window",
            )
            result = rollback_window(
                installation,
                args.window,
                service,
                runner,
                sleep,
                drop_amendments=args.drop_amendments,
            )
        else:
            if args.action in ("plan", "deploy"):
                need(
                    args.release and not (args.window or args.plan), "plan/deploy require --release"
                )
                if args.action == "deploy":
                    with _lock(installation):
                        _unfinished(installation, args.installation)
                inspector = inspect_process or (lambda unit, root: inspect_unit(unit, root, runner))
                old = discover_installation(
                    installation, runner, inspector, runtime_owner, manager=service
                )
                new = prepare_stage(
                    installation,
                    old,
                    args.release,
                    runner,
                    python=python,
                    rollback_release=args.rollback_release,
                )
                plan_dir = create_plan(installation, old, new, runner)
                if args.action == "plan":
                    print(
                        json.dumps(
                            {
                                "plan_dir": str(plan_dir),
                                "sha256": json.loads((plan_dir / "plan.json").read_bytes())[
                                    "sha256"
                                ],
                            }
                        )
                    )
                    return 0
            else:
                need(
                    args.plan and not (args.release or args.rollback_release or args.window),
                    "apply requires --plan",
                )
                plan_dir = Path(args.plan)
            digest = (
                json.loads(_private_bytes(plan_dir / "plan.json"))["sha256"]
                if args.action == "deploy"
                else None
            )
            result = apply_window(
                installation,
                plan_dir,
                service,
                runner,
                sleep,
                load=load,
                expected_digest=digest,
                input_path=args.installation,
            )
        print(json.dumps(result))
        return {
            "ACCEPTED": 0,
            "ROLLED_BACK": 2,
            "REVERTED": 2,
            "CORE_HEALTHY_MONITOR_INCOMPLETE": 3,
            "RECOVERY_REQUIRED": 4,
        }[result["outcome"]]
    except (Refused, OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print("Refused: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
