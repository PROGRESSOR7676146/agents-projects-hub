"""Explicit readonly development tools; no authority or provider orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NamespaceToolchain:
    roots: tuple[Path, ...]
    path: tuple[Path, ...]
    git_hooks_path: Path | None = None
    git_python: Path | None = None
    author_policy: Path | None = None
    git_identity: tuple[str, str] | None = None

    def environment(self) -> dict[str, str]:
        settings = {
            "gc.auto": "0",
            "maintenance.auto": "false",
            # Shared linked-worktree config is readonly. Do not switch files
            # and only then fail while implicitly installing upstream tracking.
            "branch.autoSetupMerge": "false",
            "push.autoSetupRemote": "false",
        }
        if self.git_hooks_path is not None:
            settings["core.hooksPath"] = str(self.git_hooks_path)
        if self.git_identity is not None:
            settings.update(zip(("user.name", "user.email"), self.git_identity))
        result = git_environment(settings)
        if self.git_identity is not None:
            # Hooks clear repository-routing GIT_CONFIG_* before nested Git.
            # Preserve explicit identity without leaking the outer index/config.
            for role in ("AUTHOR", "COMMITTER"):
                result[f"GIT_{role}_NAME"], result[f"GIT_{role}_EMAIL"] = self.git_identity
        if self.git_python is not None:
            result["HUB_PUBLISH_PYTHON"] = str(self.git_python)
        if self.author_policy is not None:
            result["HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE"] = str(self.author_policy)
        return result


def git_environment(settings: dict[str, str]) -> dict[str, str]:
    result = {"GIT_CONFIG_COUNT": str(len(settings))}
    for index, (key, value) in enumerate(settings.items()):
        result[f"GIT_CONFIG_KEY_{index}"] = key
        result[f"GIT_CONFIG_VALUE_{index}"] = value
    return result


def parse_toolchain(raw: Any) -> NamespaceToolchain:
    required = {"roots", "path"}
    optional = {"git_hooks_path", "git_python", "author_policy", "git_identity"}
    if not isinstance(raw, dict) or not required <= raw.keys() or raw.keys() - required - optional:
        raise ValueError("toolchain requires explicit roots/path and known options")

    def path(value: Any) -> Path:
        if (
            not isinstance(value, str)
            or not value.startswith("/")
            or ".." in Path(value).parts
            or any(ord(c) < 32 or ord(c) == 127 or c == ":" for c in value)
        ):
            raise ValueError("toolchain paths must be canonical absolute paths")
        return Path(value)

    def paths(key: str) -> tuple[Path, ...]:
        values = raw[key]
        if not isinstance(values, list) or not 1 <= len(values) <= 32:
            raise ValueError("toolchain lists must be bounded and nonempty")
        result = tuple(path(value) for value in values)
        if len(result) != len(set(result)):
            raise ValueError("toolchain lists must be unique")
        return result

    identity = raw.get("git_identity")
    if identity is not None and (
        not isinstance(identity, list)
        or len(identity) != 2
        or any(
            not isinstance(s, str)
            or not 1 <= len(s) <= 128
            or any(ord(c) < 32 or ord(c) == 127 for c in s)
            for s in identity
        )
    ):
        raise ValueError("git_identity requires a bounded printable name/email pair")
    return NamespaceToolchain(
        roots=paths("roots"),
        path=paths("path"),
        git_hooks_path=path(raw["git_hooks_path"]) if "git_hooks_path" in raw else None,
        git_python=path(raw["git_python"]) if "git_python" in raw else None,
        author_policy=path(raw["author_policy"]) if "author_policy" in raw else None,
        git_identity=None if identity is None else (identity[0], identity[1]),
    )
