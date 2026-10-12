from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hermes_codex_router.deployment_manifest import inspect_wheel  # noqa: E402


def clean_revision(root: Path) -> str:
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    status = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal", "--", "."],
        cwd=root,
        text=True,
    )
    if status:
        raise ValueError("release build requires a clean checkout")
    return revision


def seal_assets(root: Path, output: Path, revision: str) -> dict[str, Any]:
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    prefix = f"agents_projects_hub-{version}"
    wheel_name = f"{prefix}-py3-none-any.whl"
    archive_name = f"{prefix}.tar.gz"
    if {item.name for item in output.iterdir()} != {wheel_name, archive_name}:
        raise ValueError("unexpected release asset set")
    descriptor = inspect_wheel(output / wheel_name)
    if descriptor.git_sha != revision:
        raise ValueError("wheel revision does not match checkout")
    if descriptor.package_version != version:
        raise ValueError("wheel version does not match checkout")
    with tarfile.open(output / archive_name, "r:gz") as archive:
        metadata = None
        members: set[str] = set()
        for member in archive:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] != prefix
                or ".hub" in path.parts
                or ".git" in path.parts
                or not (member.isfile() or member.isdir())
                or str(path) in members
            ):
                raise ValueError("invalid source archive member")
            members.add(str(path))
            if str(path) == f"{prefix}/pyproject.toml" and member.isfile():
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("invalid source archive metadata")
                with source:
                    metadata = source.read()
        if metadata != (root / "pyproject.toml").read_bytes():
            raise ValueError("source archive metadata does not match checkout")
    for name in ("requirements-release.lock", "requirements-build.lock", "uv.lock"):
        (output / name).write_bytes((root / name).read_bytes())
    assets = []
    for name in (
        wheel_name,
        archive_name,
        "requirements-release.lock",
        "requirements-build.lock",
        "uv.lock",
    ):
        data = (output / name).read_bytes()
        assets.append({"name": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
    report = {
        "package_version": version,
        "git_sha": revision,
        "clean_tree": True,
        "built_at": descriptor.built_at,
        "schema_min": descriptor.schema_min,
        "schema_max": descriptor.schema_max,
        "artifacts": assets,
    }
    (output / "release-info.json").write_text(json.dumps(report, indent=2) + "\n")
    names = [item["name"] for item in assets] + ["release-info.json"]
    (output / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256((output / name).read_bytes()).hexdigest()}  {name}\n"
            for name in names
        )
    )
    return report


def build_assets(root: Path, output: Path) -> dict[str, Any]:
    root, output = root.resolve(), output.resolve()
    if output == root or root in output.parents:
        raise ValueError("release output must be outside the checkout")
    revision = clean_revision(root)
    if (root / "build").exists() or tuple((root / "src").glob("*.egg-info")):
        raise ValueError("release build requires a fresh checkout without stale build products")
    output.mkdir(parents=True, exist_ok=False)
    # Build the wheel directly from Git; an archive rebuild loses Git provenance.
    # Git archive retains all tracked public source, including tooling and locks.
    subprocess.run(
        [
            "uv",
            "build",
            "--wheel",
            "--python",
            sys.executable,
            "--out-dir",
            str(output),
            "--no-create-gitignore",
            "--no-build-logs",
            "--build-constraints",
            str(root / "requirements-build.lock"),
            "--require-hashes",
        ],
        cwd=root,
        check=True,
    )
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    prefix = f"agents_projects_hub-{version}"
    subprocess.run(
        [
            "git",
            "archive",
            "--format=tar.gz",
            f"--prefix={prefix}/",
            "--output",
            str(output / f"{prefix}.tar.gz"),
            revision,
        ],
        cwd=root,
        check=True,
    )
    if clean_revision(root) != revision:
        raise ValueError("checkout changed during release build")
    return seal_assets(root, output, revision)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build and seal public immutable release assets")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = build_assets(ROOT, args.output)
    print(
        json.dumps(
            {key: report[key] for key in ("package_version", "git_sha", "clean_tree", "schema_max")}
        )
    )


if __name__ == "__main__":
    main()
