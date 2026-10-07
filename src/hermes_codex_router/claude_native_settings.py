"""Fixed CLI settings shared by text-only and protected file-tool callers."""

from __future__ import annotations

import json


def disabled_builtin_plugins() -> dict[str, bool]:
    # Safe mode does not disable native built-in mods. These documented optional
    # IDs are disabled through explicit settings; this is not an allowlist of
    # plugin metadata. Required security policy mods are deliberately untouched,
    # and either stream guard still refuses any enabled/unknown plugin.
    return {
        "cc-plugin-agents-md@builtin": False,
        "cc-plugin-diff@builtin": False,
        "cc-plugin-plugin-authoring@builtin": False,
        "cc-plugin-telemetry@builtin": False,
    }


def text_only_settings() -> str:
    return json.dumps(
        {"disableAllHooks": True, "enabledPlugins": disabled_builtin_plugins()},
        separators=(",", ":"),
    )
