#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
data_root="${XDG_DATA_HOME:-$HOME/.local/share}/agents-projects-hub"
config_root="${XDG_CONFIG_HOME:-$HOME/.config}/agents-projects-hub"
systemd_root="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
hermes_root="${HERMES_HOME:-$HOME/.hermes}"

python3 -m venv "$data_root/venv"
"$data_root/venv/bin/python" -m pip install --upgrade pip
"$data_root/venv/bin/python" -m pip install "$repo_root"

install -d -m 700 "$config_root" "$systemd_root/hermes-gateway.service.d"
if [[ ! -e "$config_root/hub.json" ]]; then
  install -m 600 "$repo_root/config/hub.example.json" "$config_root/hub.json"
fi
if [[ ! -e "$config_root/projects.json" ]]; then
  install -m 600 "$repo_root/config/projects.example.json" "$config_root/projects.json"
fi
if [[ ! -e "$config_root/environment" ]]; then
  install -m 600 "$repo_root/config/environment.example" "$config_root/environment"
fi

install -m 644 "$repo_root/systemd/agents-projects-hub.service" \
  "$systemd_root/agents-projects-hub.service"
install -m 644 "$repo_root/systemd/agents-projects-hub@.service" \
  "$systemd_root/agents-projects-hub@.service"
install -m 644 "$repo_root/systemd/agents-projects-hub-monitor.service" \
  "$systemd_root/agents-projects-hub-monitor.service"
install -m 644 "$repo_root/systemd/agents-projects-hub-monitor.timer" \
  "$systemd_root/agents-projects-hub-monitor.timer"
if [[ ! -e "$systemd_root/tlive.service" ]]; then
  install -m 644 "$repo_root/systemd/tlive.service" "$systemd_root/tlive.service"
fi
install -m 644 "$repo_root/systemd/hermes-gateway.service.d/20-agents-projects-hub.conf" \
  "$systemd_root/hermes-gateway.service.d/20-agents-projects-hub.conf"

install -d -m 700 \
  "$hermes_root/plugins/hermes-project-hub" \
  "$hermes_root/hooks/hermes-project-hub-turn-export"
install -m 600 "$repo_root/integrations/hermes-project-hub/__init__.py" \
  "$hermes_root/plugins/hermes-project-hub/__init__.py"
install -m 600 "$repo_root/integrations/hermes-project-hub/plugin.yaml" \
  "$hermes_root/plugins/hermes-project-hub/plugin.yaml"
install -m 600 "$repo_root/integrations/hermes-project-hub-hook/handler.py" \
  "$hermes_root/hooks/hermes-project-hub-turn-export/handler.py"
install -m 600 "$repo_root/integrations/hermes-project-hub-hook/HOOK.yaml" \
  "$hermes_root/hooks/hermes-project-hub-turn-export/HOOK.yaml"

systemctl --user daemon-reload

printf '%s\n' \
  "Installed Agents Projects Hub." \
  "Edit $config_root/projects.json, hub.json, and environment." \
  "Then run: $data_root/venv/bin/agents-projects-hub doctor $config_root/hub.json" \
  "Enable only after doctor succeeds: systemctl --user enable --now agents-projects-hub.service"
