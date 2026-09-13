from __future__ import annotations

import argparse
import json
import re
import subprocess
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

_SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
_CHANGELOG_RELEASE = re.compile(
    r"^## \[((?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*))\]", re.MULTILINE
)
_STATUS_RELEASE = re.compile(r"^Release: v([^\s]+)$", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class ReleaseMetadataAudit:
    version: str
    errors: tuple[str, ...]
    debts: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def _git_lines(root: Path, *args: str) -> tuple[str, ...]:
    completed = subprocess.run(
        ("git", *args),
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("unable to inspect local Git tags")
    return tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())


def audit_release_metadata(
    root: Path,
    *,
    tags: Sequence[str] | None = None,
    head_tags: Sequence[str] | None = None,
) -> ReleaseMetadataAudit:
    root = root.resolve()
    with (root / "pyproject.toml").open("rb") as stream:
        package_version = str(tomllib.load(stream)["project"]["version"])
    status_text = (root / "docs" / "status" / "PROJECT_STATUS.md").read_text(encoding="utf-8")
    changelog_text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    errors: list[str] = []
    if _SEMVER.fullmatch(package_version) is None:
        errors.append(f"package version is not canonical SemVer: {package_version}")

    status_match = _STATUS_RELEASE.search(status_text)
    if status_match is None:
        errors.append("project status must declare Release: v<SemVer>")
    elif status_match.group(1) != package_version:
        errors.append(
            f"project status release v{status_match.group(1)} does not match "
            f"package version {package_version}"
        )

    headings = re.findall(r"^## \[([^]]+)\]", changelog_text, re.MULTILINE)
    if not headings or headings[0] != "Unreleased":
        errors.append("changelog must begin with an Unreleased section")
    releases = tuple(_CHANGELOG_RELEASE.findall(changelog_text))
    if not releases:
        errors.append("changelog has no SemVer release entry")
    elif releases[0] != package_version:
        errors.append(
            f"latest changelog release {releases[0]} does not match "
            f"package version {package_version}"
        )
    duplicates = sorted({version for version in releases if releases.count(version) > 1})
    for duplicate in duplicates:
        errors.append(f"duplicate changelog release: {duplicate}")

    known_tags = tuple(tags) if tags is not None else _git_lines(root, "tag", "--list", "v*")
    current_tags = (
        tuple(head_tags)
        if head_tags is not None
        else _git_lines(root, "tag", "--points-at", "HEAD", "v*")
    )
    release_set = set(releases)
    for tag in sorted(set(known_tags)):
        tagged_version = tag.removeprefix("v")
        if _SEMVER.fullmatch(tagged_version) is None:
            errors.append(f"release tag is not canonical v<SemVer>: {tag}")
        elif tagged_version not in release_set:
            errors.append(f"release tag {tag} has no changelog entry")
    expected_tag = f"v{package_version}"
    for tag in sorted(set(current_tags)):
        if tag != expected_tag:
            errors.append(f"HEAD release tag {tag} does not match package version {expected_tag}")

    debts = tuple(
        f"missing release tag: v{release}"
        for release in releases
        if f"v{release}" not in known_tags
    )
    return ReleaseMetadataAudit(package_version, tuple(errors), debts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="audit synchronized release metadata")
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    result = audit_release_metadata(parser.parse_args(argv).root)
    print(json.dumps({"ok": result.ok, **asdict(result)}, indent=2))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
