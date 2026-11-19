#!/usr/bin/env python3
"""Verify a published recovery capsule without trusting its symlink or receipt."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sys


def digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


_GIT_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
_RELEASE = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")


def verify(
    root: pathlib.Path,
    capsule_id: str,
    now: dt.datetime | None = None,
    *,
    expected_source_git_sha: str | None = None,
    expected_package_version: str | None = None,
    expected_state_schema: int | None = None,
) -> list[str]:
    """Verify capsule freshness, and optionally its exact release identity.

    Without all three ``expected_*`` inputs this proves only the published
    capsule's internal hashes and freshness.  It does not prove compatibility
    with a particular release or installed deployment.
    """
    errors: list[str] = []
    expected = (expected_source_git_sha, expected_package_version, expected_state_schema)
    if any(value is not None for value in expected) and not all(
        value is not None for value in expected
    ):
        return [
            "expected recovery capsule identity must include source SHA, package release, and state schema"
        ]
    if all(value is not None for value in expected) and (
        not isinstance(expected_source_git_sha, str)
        or _GIT_SHA.fullmatch(expected_source_git_sha) is None
        or not isinstance(expected_package_version, str)
        or _RELEASE.fullmatch(expected_package_version) is None
        or type(expected_state_schema) is not int
        or expected_state_schema < 1
    ):
        return ["expected recovery capsule identity is invalid"]
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
    maintained_release = manifest.get("maintained_release")
    state_schema = manifest.get("state_schema")
    if (
        not isinstance(maintained_release, str)
        or _RELEASE.fullmatch(maintained_release) is None
        or type(state_schema) is not int
        or state_schema < 1
    ):
        errors.append("capsule release metadata is invalid")
    elif (
        receipt.get("maintained_release") != maintained_release
        or receipt.get("state_schema") != state_schema
    ):
        errors.append("capsule receipt does not bind its release metadata")
    if all(value is not None for value in expected):
        if receipt.get("source_git_sha") != expected_source_git_sha:
            errors.append("capsule source revision does not match expected release identity")
        if maintained_release != expected_package_version:
            errors.append("capsule package release does not match expected release identity")
        if state_schema != expected_state_schema:
            errors.append("capsule state schema does not match expected release identity")
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


def main(argv: list[str] | None = None) -> int:
    default = pathlib.Path(os.environ.get("XDG_DATA_HOME", pathlib.Path.home() / ".local/share"))
    parser = argparse.ArgumentParser()
    parser.add_argument("capsule_id", nargs="?", default="agents-projects-hub")
    parser.add_argument("--root", type=pathlib.Path, default=default / "recovery-capsules")
    parser.add_argument("--expected-source-git-sha")
    parser.add_argument("--expected-package-version")
    parser.add_argument("--expected-state-schema", type=int)
    args = parser.parse_args(argv)
    errors = verify(
        args.root,
        args.capsule_id,
        expected_source_git_sha=args.expected_source_git_sha,
        expected_package_version=args.expected_package_version,
        expected_state_schema=args.expected_state_schema,
    )
    for error in errors:
        print(f"FAIL: {error}")
    if errors:
        return 1
    if args.expected_source_git_sha is None:
        print(
            f"OK: recovery capsule {args.capsule_id} hashes and freshness are verified; "
            "deployment compatibility was not checked"
        )
    else:
        print(
            f"OK: recovery capsule {args.capsule_id} hashes, freshness, and expected release "
            "identity are verified"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
