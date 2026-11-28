#!/usr/bin/env python3
"""Read-only admission ticket for an exact prepared same-schema Hub release."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

try:
    from scripts import hub_deploy as deploy
except ImportError:  # python -I retains neither the checkout nor the script directory
    spec = importlib.util.spec_from_file_location(
        "hub_deploy", Path(__file__).with_name("hub_deploy.py")
    )
    assert spec is not None and spec.loader is not None
    deploy = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(deploy)

RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}")


def digest(value):
    return hashlib.sha256(deploy.canonical_document(value)).hexdigest()


def file_ref(path):
    deploy.need(
        str(path.resolve(strict=True)) == str(path) and path.is_file(),
        "invalid admission input file",
    )
    with path.open("rb") as stream:
        return {"path": str(path), "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}


def runtime_json(runtime, runner, *arguments):
    return deploy._json_command(
        runner,
        [
            str(Path(runtime["root"]) / "venv/bin/python"),
            "-I",
            "-B",
            "-m",
            "hermes_codex_router.cli",
            *arguments,
        ],
    )


def release(plan, label, runner):
    runtime = plan[label + "_runtime"]
    wheel = Path(plan[label + "_wheel"])
    tag = "v" + wheel.name.removeprefix("agents_projects_hub-").removesuffix("-py3-none-any.whl")
    catalog = file_ref(wheel.parent / "SHA256SUMS")
    deploy.need(
        plan["asset_catalogs"][label] == catalog["sha256"],
        "release asset catalog changed",
    )
    info, verified = deploy.verify_assets(wheel.parent, tag)
    deploy.need(
        verified == wheel
        and deploy._wheel_digest(info, wheel) == plan[label + "_wheel_sha256"]
        and info["git_sha"] == runtime["git_sha"]
        and info["schema_max"] == plan["state_schema"],
        "release assets differ from the plan",
    )
    root = Path(runtime["root"])
    runtime_pin = runtime["tree_sha256"]
    deploy.need(
        isinstance(runtime_pin, str) and deploy._HEX.fullmatch(runtime_pin),
        "sealed runtime pin missing from the plan",
    )
    deploy._runtime_command(root, "verify", runner, runtime_pin)
    running = runtime_json(runtime, runner, "release-info")
    deploy.need(
        all(running.get(k) == info[k] for k in ("git_sha", "clean_tree", "package_version")),
        "release runtime differs from the plan",
    )
    return {
        "tag": tag,
        "git_sha": info["git_sha"],
        "wheel": {"path": str(wheel), "sha256": plan[label + "_wheel_sha256"]},
        "runtime": {"path": str(root), "sha256": runtime_pin},
        "assets": {
            **{a["name"]: a["sha256"] for a in info["artifacts"]},
            "release-info.json": file_ref(wheel.parent / "release-info.json")["sha256"],
            "SHA256SUMS": catalog["sha256"],
        },
    }


def effective_argv(unit, runner):
    (commands,) = deploy._systemd_properties(
        runner, unit, ("Service", "ExecStartEx", "a(sasasttttuii)")
    )
    deploy.need(isinstance(commands, list) and len(commands) == 1, "unsupported service launch")
    executable, argv, flags = commands[0][:3]
    deploy.need(
        not flags
        and argv
        and argv[0] == executable
        and all(
            isinstance(arg, str) and not any(c in arg for c in ("\0", "$", "%")) for arg in argv
        ),
        "unsupported service arguments",
    )
    return argv


def consumers(plan, source, runner):
    installation = plan["installation"]
    old_cli = deploy._runtime_cli(plan["old_runtime"])
    new_cli = deploy._runtime_cli(plan["new_runtime"])
    units = [unit for unit, mode in plan["unit_runtime_modes"].items() if mode == "process"]
    units.append(installation["monitor_service"])
    before, after = {}, {}
    for unit in units:
        argv = effective_argv(unit, runner)
        deploy.need(
            len(argv) >= 3
            and argv[0] == old_cli
            and argv[2] in {source, installation["hub_config"]},
            "unsupported Hub consumer configuration",
        )
        deploy.need(
            not any(
                plan["old_runtime"]["root"] in arg or Path(source).name in arg for arg in argv[3:]
            ),
            "unsupported release reference in service arguments",
        )
        candidate = [new_cli, *argv[1:]]
        if argv[2] == source:
            candidate[2] = plan["candidate_config"]
        before[unit] = {"configuration": argv[2], "argv_sha256": digest(argv)}
        after[unit] = {"configuration": candidate[2], "argv_sha256": digest(candidate)}
    return before, after


def installed_manifest(installation, plan, previous, runner):
    status = deploy.window_status(installation)
    deploy.need(
        status and status["state"] == "ACCEPTED" and "manifest" in status,
        "accepted installation manifest required",
    )
    path = Path(status["manifest"])
    descriptor = file_ref(path)
    manifest = runtime_json(
        plan["old_runtime"],
        runner,
        "release-manifest",
        "verify",
        str(path),
        "--state",
        installation["state_db"],
    )
    active = manifest["active_artifact"]
    deploy.need(
        manifest.get("ok") is True
        and active["git_sha"] == previous["git_sha"]
        and active["sha256"] == previous["wheel"]["sha256"]
        and manifest["configuration"] == file_ref(Path(installation["hub_config"]))
        and manifest["state_backup"]["schema_version"] == plan["state_schema"]
        and descriptor == file_ref(path),
        "accepted manifest differs from the live baseline",
    )
    return {
        "installation_manifest": descriptor,
        "active_artifact": active,
        "rollback_artifact": manifest["rollback_artifact"],
        "state_backup": manifest["state_backup"],
    }


def project(installation, plan_dir, expected_plan, runner=deploy.run_command):
    """Project installed and expected backup inputs without opening a window."""
    deploy.need(
        plan_dir.parent == Path(installation["ops_dir"]) and plan_dir.name.startswith("plan-"),
        "plan must be in the ops directory",
    )
    plan = deploy.verify_saved_plan(plan_dir)
    deploy.need(digest(plan) == expected_plan, "plan digest changed")
    deploy.need(plan["installation"] == installation, "installation changed")
    schema = plan["state_schema"]
    deploy.need(
        schema == plan["state_schema_before"] == deploy._live_schema(installation)
        and not any(key.startswith("rollback_") for key in plan),
        "nightly admission requires the installed schema and rollback release",
    )
    deploy.need(not deploy.store_mode(installation), "Claude CLI store requires separate admission")
    deploy.need("asset_catalogs" in plan and "service_inputs" in plan, "nightly plan required")
    deploy.need(
        deploy._nightly_service_inputs(installation, runner) == plan["service_inputs"],
        "loaded service inputs differ from the plan",
    )
    _, dropins = deploy._managed_dropins(installation, plan["old_runtime"]["root"])
    deploy.need(
        {str(path) for path in dropins}
        == {entry["path"] for entry in plan["files"] if entry["kind"] == "dropin"}
        and deploy._unit_runtime_modes(installation, plan["old_runtime"]["root"])
        == plan["unit_runtime_modes"],
        "managed service inventory differs from the plan",
    )
    deploy.need(
        not any(entry["kind"] == "base_unit" for entry in plan["files"]),
        "nightly admission requires unchanged unit templates",
    )
    old = release(plan, "old", runner)
    new = release(plan, "new", runner)
    deploy.need(
        old["git_sha"] != new["git_sha"]
        and tuple(map(int, old["tag"][1:].split("."))) < tuple(map(int, new["tag"][1:].split("."))),
        "nightly admission requires a forward release",
    )
    source = next(
        (entry for entry in plan["files"] if entry["kind"] == "source_config"),
        next(entry for entry in plan["files"] if entry["kind"] == "claude_config"),
    )
    worker = next(entry for entry in plan["files"] if entry["kind"] == "claude_config")
    hub = deploy._hub_entry(plan)
    deploy.need(hub["before"]["sha256"] == hub["after"]["sha256"], "primary configuration changed")
    manifest = installed_manifest(installation, plan, old, runner)
    inventory = runtime_json(
        plan["new_runtime"],
        runner,
        "release-manifest",
        "backup-inputs",
        hub["path"],
        source["path"],
        str(plan_dir / worker["after"]["copy"]),
    )
    databases = inventory.get("databases")
    deploy.need(
        inventory.get("ok") is True
        and inventory.get("projection_version") == 1
        and isinstance(databases, dict)
        and databases["shared"]["path"] == installation["state_db"]
        and all(db["schema_version"] == schema for db in databases.values()),
        "nightly backup database inventory differs",
    )
    before_units, after_units = consumers(plan, source["path"], runner)
    common = {
        "projection_version": 1,
        "schema": schema,
        "databases": databases,
        "registry": inventory["registry"],
    }
    primary = {"path": hub["path"], "sha256": hub["before"]["sha256"]}
    before = {
        **common,
        **manifest,
        "primary_configuration": primary,
        "worker_configuration": {"path": source["path"], "sha256": source["before"]["sha256"]},
        "consumers": before_units,
    }
    body = {
        "kind": "hub-nightly-admission",
        "version": 1,
        "plan_sha256": expected_plan,
        "before": before,
        "before_sha256": digest(before),
        "target": new,
        "rollback": old,
        "after": {
            **common,
            "primary_configuration": primary,
            "worker_configuration": {"path": worker["path"], "sha256": worker["after"]["sha256"]},
            "consumers": after_units,
        },
        "service_inputs": plan["service_inputs"],
        "service_inventory": {
            key: installation[key]
            for key in ("restart_order", "untouched_units", "config_only_units", "monitor_timer")
        },
        "files": [
            {
                "path": entry["path"],
                "kind": entry["kind"],
                **{
                    role: entry[role]["sha256"] if entry[role] else None
                    for role in ("before", "after")
                },
            }
            for entry in plan["files"]
        ],
    }
    deploy.need(
        digest(deploy.verify_saved_plan(plan_dir)) == expected_plan, "plan changed during readback"
    )
    return {"ticket": body, "sha256": digest(body)}


def private_json(path, limit):
    def unique(pairs):
        value = dict(pairs)
        deploy.need(len(value) == len(pairs), "duplicate backup evidence fields")
        return value

    deploy.need(
        path == path.resolve(strict=True)
        and path.is_file()
        and path.stat().st_nlink == 1
        and path.stat().st_size <= limit,
        "unsafe backup evidence",
    )
    data = deploy._private_bytes(path)
    value = json.loads(data, object_pairs_hook=unique)
    deploy.need(isinstance(value, dict), "invalid backup evidence")
    return value, hashlib.sha256(data).hexdigest()


def verify_backup(runs_dir, run_id, baseline_sha256, *, run_started_at, now=time.time):
    """Read the successful run selected by the current invocation's runner."""
    deploy.need(RUN_ID.fullmatch(run_id), "invalid backup run ID")
    dated = datetime.strptime(run_id.split("-", 1)[0], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    deploy.need(
        type(run_started_at) in (int, float) and 0 < run_started_at < math.inf,
        "missing backup invocation start",
    )
    run = runs_dir / run_id
    for path in (runs_dir, run, run / "bundle"):
        info = path.lstat()
        deploy.need(
            path.resolve() == path
            and stat.S_ISDIR(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700,
            "unsafe backup result directory",
        )
    success = run / "SUCCESS.json"
    failure = run / "FAILURE.json"
    deploy.need(
        success.is_file() and not (failure.exists() or failure.is_symlink()),
        "backup success receipt missing",
    )
    receipt, receipt_digest = private_json(success, 64 * 1024)
    snapshot_ids = [receipt.get(key) for key in ("hub_snapshot", "home_snapshot")]
    completed = receipt.get("completed_at")
    deploy.need(
        receipt.get("run_id") == run_id
        and all(isinstance(item, str) and deploy._HEX.fullmatch(item) for item in snapshot_ids)
        and snapshot_ids[0] != snapshot_ids[1]
        and type(completed) in (int, float)
        and max(dated.timestamp(), run_started_at) <= completed <= now()
        and dated.timestamp() >= math.floor(run_started_at),
        "backup success receipt differs",
    )
    coverage = receipt.get("attachment_coverage")
    deploy.need(
        isinstance(coverage, dict)
        and coverage.get("satisfies_update_artifact_precondition") is True,
        "backup artifact coverage is incomplete",
    )
    manifest, manifest_digest = private_json(run / "bundle/manifest.json", 1024 * 1024)
    deploy.need(
        manifest_digest == receipt.get("manifest_sha256"), "backup archive manifest changed"
    )
    deploy.need(
        type(manifest.get("format")) is int
        and manifest["format"] == 1
        and isinstance(baseline_sha256, str)
        and deploy._HEX.fullmatch(baseline_sha256)
        and manifest.get("hub_baseline_sha256") == baseline_sha256,
        "archived baseline does not match the ticket",
    )
    return receipt_digest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installation", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--plan-sha256", required=True)
    parser.add_argument("--ticket-sha256")
    args = parser.parse_args(argv)
    try:
        result = project(deploy.load_installation(args.installation), args.plan, args.plan_sha256)
        deploy.need(
            args.ticket_sha256 is None or args.ticket_sha256 == result["sha256"],
            "admission ticket changed",
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        subprocess.SubprocessError,
    ) as exc:
        print(
            f"Refused: {exc if isinstance(exc, deploy.Refused) else type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    print(deploy.canonical_document(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
