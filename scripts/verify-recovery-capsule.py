#!/usr/bin/env python3
"""Verify a published recovery capsule without trusting its symlink or receipt."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(root: pathlib.Path, capsule_id: str, now: dt.datetime | None = None) -> list[str]:
    errors: list[str] = []
    capsule_root = (root / capsule_id).resolve()
    current = root / capsule_id / "current"
    try:
        generation = current.resolve(strict=True)
        generation.relative_to(capsule_root / "versions")
    except (FileNotFoundError, RuntimeError, ValueError):
        return ["current generation is missing or escapes the capsule root"]
    try:
        receipt = json.loads((generation / "receipt.json").read_text())
        manifest = json.loads((generation / "manifest.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"capsule metadata is unreadable: {type(exc).__name__}"]
    if receipt.get("capsule_id") != capsule_id or manifest.get("capsule_id") != capsule_id:
        errors.append("capsule identity mismatch")
    if receipt.get("source_clean") is not True:
        errors.append("capsule was not published from a clean source tree")
    if generation.name != receipt.get("source_git_sha"):
        errors.append("generation name differs from source revision")
    for name in ("manifest.json", "RUNBOOK.md"):
        expected = receipt.get("files", {}).get(name)
        path = generation / name
        if not expected or not path.is_file() or digest(path) != expected:
            errors.append(f"content hash mismatch: {name}")
    try:
        published = dt.datetime.fromisoformat(receipt["published_at"])
        if published.tzinfo is None:
            raise ValueError
        age = (now or dt.datetime.now(dt.timezone.utc)) - published
        if age.total_seconds() < -300:
            errors.append("publication timestamp is in the future")
        elif age > dt.timedelta(days=int(manifest["max_age_days"])):
            errors.append("capsule is stale")
    except (KeyError, TypeError, ValueError):
        errors.append("publication time or maximum age is invalid")
    return errors


def main() -> int:
    default = pathlib.Path(os.environ.get("XDG_DATA_HOME", pathlib.Path.home() / ".local/share"))
    parser = argparse.ArgumentParser()
    parser.add_argument("capsule_id", nargs="?", default="agents-projects-hub")
    parser.add_argument("--root", type=pathlib.Path, default=default / "recovery-capsules")
    args = parser.parse_args()
    errors = verify(args.root, args.capsule_id)
    for error in errors:
        print(f"FAIL: {error}")
    if errors:
        return 1
    print(f"OK: recovery capsule {args.capsule_id} is authentic and current")
    return 0


if __name__ == "__main__":
    sys.exit(main())
