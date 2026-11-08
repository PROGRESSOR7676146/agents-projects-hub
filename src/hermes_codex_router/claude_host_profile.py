"""Explicit unconfined Claude launch selected by local configuration."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ClaudeHostConfig:
    project_root: Path
    provider_home: Path
    claude_executable: Path
    profile: str = "host"


def host_event_policy(event: dict[str, object]) -> None:
    """Keep stream parsing and bounds without rejecting native tool kinds."""


def validate_host_route_settings(root: Path, session_home: Path) -> None:
    """Reject settings that can select credentials while retaining other customizations."""
    import json

    candidates = [session_home / "settings.json", Path("/etc/claude-code/managed-settings.json")]
    for directory in (root, *root.parents):
        candidates.extend(
            (directory / ".claude/settings.json", directory / ".claude/settings.local.json")
        )
    for path in candidates:
        if not path.exists() and not path.is_symlink():
            continue
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError) as error:
            raise ValueError("Claude settings could not be verified") from error
        if not isinstance(settings, dict):
            raise ValueError("Claude settings could not be verified")
        env = settings.get("env", {})
        if not isinstance(env, dict) or "apiKeyHelper" in settings:
            raise ValueError("Claude settings can select credentials")
        if any(
            isinstance(key, str)
            and (
                key in {"HOME", "CLAUDE_CONFIG_DIR", "GOOGLE_APPLICATION_CREDENTIALS"}
                or key.startswith("ANTHROPIC_")
                or key.startswith("CLAUDE_CODE_USE_")
                or key.startswith("CLAUDE_CODE_OAUTH_TOKEN")
                or key.startswith(("AWS_", "AZURE_", "GOOGLE_CLOUD_"))
            )
            for key in env
        ):
            raise ValueError("Claude settings can select credentials")
