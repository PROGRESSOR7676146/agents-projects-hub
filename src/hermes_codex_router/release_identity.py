from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from ._build_info import BUILT_AT, CLEAN_TREE, GIT_SHA, PACKAGE_VERSION

_GIT_SHA = re.compile(r"[0-9a-f]{40,64}")


@dataclass(frozen=True, slots=True)
class ReleaseIdentity:
    package_version: str
    git_sha: str | None
    built_at: str | None
    clean_tree: bool

    def __post_init__(self) -> None:
        if not self.package_version or len(self.package_version) > 64:
            raise ValueError("invalid package version")
        if self.git_sha is not None and _GIT_SHA.fullmatch(self.git_sha) is None:
            raise ValueError("invalid release Git SHA")
        if self.built_at is not None:
            try:
                parsed = datetime.fromisoformat(self.built_at)
            except ValueError as exc:
                raise ValueError("invalid release build time") from exc
            if parsed.tzinfo is None:
                raise ValueError("release build time must be timezone-aware")
        if self.clean_tree and (self.git_sha is None or self.built_at is None):
            raise ValueError("clean release identity must be complete")

    @property
    def verified(self) -> bool:
        return self.clean_tree and self.git_sha is not None and self.built_at is not None


CURRENT_RELEASE = ReleaseIdentity(PACKAGE_VERSION, GIT_SHA, BUILT_AT, CLEAN_TREE)
