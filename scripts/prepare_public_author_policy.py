#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_VALUE_ENV = "HUB_PUBLIC_GIT_AUTHOR_EMAIL"
_PATH_ENV = "HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE"
_MAX_BYTES = 320


def _external_directory(name: str) -> Path | None:
    raw = os.environ.get(name)
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(ROOT.resolve(strict=True))
    except ValueError:
        return resolved
    except OSError:
        return None
    return None


def main() -> int:
    value = os.environ.get(_VALUE_ENV)
    runner_temp = _external_directory("RUNNER_TEMP")
    github_env_raw = os.environ.get("GITHUB_ENV")
    if value is None or runner_temp is None or not github_env_raw:
        return 0
    github_env = Path(github_env_raw)
    if not github_env.is_absolute():
        return 0
    raw = value.encode("utf-8")
    if len(raw) > _MAX_BYTES:
        return 0
    descriptor: int | None = None
    policy_path: Path | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix="hub-public-author-",
            dir=runner_temp,
        )
        policy_path = Path(temporary)
        with os.fdopen(descriptor, "wb") as policy:
            os.fchmod(descriptor, 0o600)
            policy.write(raw)
        descriptor = None
        with github_env.open("a", encoding="utf-8") as environment:
            environment.write(f"{_PATH_ENV}={policy_path}\n")
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if policy_path is not None:
            try:
                policy_path.unlink()
            except OSError:
                pass
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
