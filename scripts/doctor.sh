#!/usr/bin/env bash
set -euo pipefail

config_path="${1:-${XDG_CONFIG_HOME:-$HOME/.config}/agents-projects-hub/hub.json}"
if command -v agents-projects-hub >/dev/null 2>&1; then
  exec agents-projects-hub doctor "$config_path"
fi

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHONPATH="$repo_root/src" exec python3 -m hermes_codex_router.cli doctor "$config_path"
