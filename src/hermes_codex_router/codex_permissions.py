"""Named Codex policy selection; metadata is not a filesystem attestation."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from .codex_failure import CodexPermissionPolicyDriftError, CodexPermissionProfileError

if TYPE_CHECKING:
    from .hub_config import HubConfig

__all__ = [
    "CodexPermissionBinding",
    "CodexPermissionPolicyDriftError",
    "CodexPermissionProfileError",
    "MissingPermissionContext",
    "MISSING_PERMISSION_CONTEXT",
    "MANAGED_LOCAL_REFUSAL",
    "validate_managed_execution_mode",
    "validate_permission_profile_id",
    "verify_managed_selection",
]


class MissingPermissionContext(Enum):
    MISSING = "missing"


MISSING_PERMISSION_CONTEXT = MissingPermissionContext.MISSING

MANAGED_LOCAL_REFUSAL = (
    "Local Codex transfer is unavailable for a managed permission profile until "
    "the local execution boundary is verified. Telegram retains ownership."
)


def validate_managed_execution_mode(config: HubConfig) -> None:
    """Named selection is supported only by the durable external Codex worker."""
    if config.codex_permission_profile is not None and not (
        config.dispatch_mode == "queue"
        and config.queue_runtime == "external"
        and "codex" in (config.external_worker_agent_ids or ("codex",))
    ):
        raise ValueError("managed Codex permission profiles require an external Codex queue worker")


def validate_permission_profile_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value) is None:
        raise ValueError("Codex permission profile must be a bounded named identifier")
    return value


class MetadataRequest(Protocol):
    def __call__(
        self, method: str, params: dict[str, Any], *, deadline: float | None = None
    ) -> Any: ...


def verify_managed_selection(request: MetadataRequest, profile: str, cwd: Path) -> None:
    """Bounded, model-free metadata checks on the connection preparing the thread."""
    deadline = time.monotonic() + 10.0
    cursor: str | None = None
    cursors: set[str] = set()
    profiles: dict[str, bool] = {}
    try:
        for _ in range(10):
            params: dict[str, Any] = {"cwd": str(cwd), "limit": 50}
            if cursor is not None:
                params["cursor"] = cursor
            response = request("permissionProfile/list", params, deadline=deadline)
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, list) or len(data) > 50:
                raise CodexPermissionProfileError()
            for item in data:
                if not isinstance(item, dict):
                    raise CodexPermissionProfileError()
                identity, allowed = item.get("id"), item.get("allowed")
                if (
                    not isinstance(identity, str)
                    or not 1 <= len(identity) <= 128
                    or type(allowed) is not bool
                    or identity in profiles
                ):
                    raise CodexPermissionProfileError()
                profiles[identity] = allowed
            cursor = response.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048 or cursor in cursors:
                raise CodexPermissionProfileError()
            cursors.add(cursor)
        else:
            raise CodexPermissionProfileError()
        if {identity for identity, allowed in profiles.items() if allowed} != {profile}:
            raise CodexPermissionProfileError()
        response = request("configRequirements/read", {}, deadline=deadline)
        requirements = response.get("requirements") if isinstance(response, dict) else None
        if not isinstance(requirements, dict) or requirements.get("defaultPermissions") != profile:
            raise CodexPermissionProfileError()
        allowed_profiles = requirements.get("allowedPermissionProfiles")
        if (
            not isinstance(allowed_profiles, dict)
            or not all(
                isinstance(key, str) and type(value) is bool
                for key, value in allowed_profiles.items()
            )
            or {key for key, allowed in allowed_profiles.items() if allowed} != {profile}
        ):
            raise CodexPermissionProfileError()
    except CodexPermissionProfileError:
        raise
    except Exception as error:
        raise CodexPermissionProfileError() from error


@dataclass(frozen=True, slots=True)
class CodexPermissionBinding:
    thread_id: str
    root: Path
    profile: str
    approval_policy: str
    model_provider: str

    def validate(self, settings: object) -> None:
        if not isinstance(settings, dict):
            raise CodexPermissionProfileError()
        profile = settings.get("activePermissionProfile")
        if not (
            isinstance(profile, dict)
            and profile.get("id") == self.profile
            # Managed definitions are intentionally opaque: native metadata
            # reports a null parent even when requirements extends :workspace.
            and "extends" in profile
            and profile["extends"] in (None, ":workspace")
            and settings.get("approvalPolicy") == self.approval_policy
            and settings.get("approvalsReviewer") == "user"
            and settings.get("modelProvider") == self.model_provider
        ):
            raise CodexPermissionProfileError()
        cwd = settings.get("cwd")
        try:
            if (
                not isinstance(cwd, str)
                or not Path(cwd).is_absolute()
                or Path(cwd).resolve(strict=True) != self.root
            ):
                raise CodexPermissionProfileError()
        except (OSError, RuntimeError) as error:
            raise CodexPermissionProfileError() from error
        # Reject visible broadening; this projection still does not reveal the
        # complete managed definition or read boundaries.
        sandbox = settings.get("sandboxPolicy", settings.get("sandbox"))
        if not (
            isinstance(sandbox, dict)
            and sandbox.get("type") == "workspaceWrite"
            and sandbox.get("networkAccess") is False
            and sandbox.get("excludeTmpdirEnvVar") is True
            and sandbox.get("excludeSlashTmp") is True
        ):
            raise CodexPermissionProfileError()
        roots = sandbox.get("writableRoots")
        if not isinstance(roots, list) or len(roots) > 64:
            raise CodexPermissionProfileError()
        for root in roots:
            try:
                if (
                    not isinstance(root, str)
                    or not Path(root).is_absolute()
                    or not Path(root).resolve(strict=True).is_relative_to(self.root)
                ):
                    raise CodexPermissionProfileError()
            except (OSError, RuntimeError) as error:
                raise CodexPermissionProfileError() from error
