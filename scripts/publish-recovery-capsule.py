#!/usr/bin/env python3
"""Publish this repository's recovery capsule into a neutral local store."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "recovery/agents-projects-hub"


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def git(*args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], check=True, text=True, capture_output=True
    ).stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    default = pathlib.Path(os.environ.get("XDG_DATA_HOME", pathlib.Path.home() / ".local/share"))
    parser.add_argument("--destination", type=pathlib.Path, default=default / "recovery-capsules")
    parser.add_argument("--allow-dirty", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    dirty = bool(git("status", "--porcelain"))
    if dirty and not args.allow_dirty:
        raise SystemExit("refusing to publish a recovery capsule from a dirty tree")
    revision = git("rev-parse", "HEAD")
    capsule_id = json.loads((SOURCE / "manifest.json").read_text())["capsule_id"]
    root = args.destination / capsule_id
    version = root / "versions" / revision
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    (root / "versions").mkdir(mode=0o700, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="recovery-capsule-", dir=root / "versions") as tmp:
        staged = pathlib.Path(tmp)
        for name in ("manifest.json", "RUNBOOK.md"):
            shutil.copy2(SOURCE / name, staged / name)
        receipt = {
            "schema_version": 1,
            "capsule_id": capsule_id,
            "source_git_sha": revision,
            "source_clean": not dirty,
            "published_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "files": {name: sha256(staged / name) for name in ("manifest.json", "RUNBOOK.md")},
        }
        (staged / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        if not version.exists():
            pathlib.Path(tmp).rename(version)

    link = root / "current"
    temporary_link = root / f".current-{os.getpid()}"
    temporary_link.symlink_to(pathlib.Path("versions") / revision)
    temporary_link.replace(link)
    print(
        json.dumps(
            {"ok": True, "capsule": capsule_id, "revision": revision, "path": str(link)}, indent=2
        )
    )


if __name__ == "__main__":
    main()
