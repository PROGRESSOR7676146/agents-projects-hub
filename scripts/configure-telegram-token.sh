#!/usr/bin/env bash
set -euo pipefail

agent_id="${1:-}"
if [[ ! "$agent_id" =~ ^[a-z][a-z0-9_-]{0,47}$ ]]; then
  printf 'usage: %s AGENT_ID\n' "$0" >&2
  exit 2
fi

secrets_root="${XDG_CONFIG_HOME:-$HOME/.config}/agents-projects-hub/secrets"
token_path="$secrets_root/$agent_id-telegram-token"
if [[ -e "$token_path" ]]; then
  printf 'refusing to overwrite existing token file: %s\n' "$token_path" >&2
  exit 2
fi

install -d -m 700 "$secrets_root"
read -r -s -p "Paste BotFather token for $agent_id: " bot_token
printf '\n' >&2
if [[ ! "$bot_token" =~ ^[0-9]{6,}:[A-Za-z0-9_-]{20,}$ ]]; then
  unset bot_token
  printf 'token format is invalid\n' >&2
  exit 2
fi

umask 077
temporary="$(mktemp "$secrets_root/.$agent_id.XXXXXX")"
trap 'rm -f "$temporary"; unset bot_token' EXIT
printf '%s\n' "$bot_token" >"$temporary"
chmod 600 "$temporary"
mv "$temporary" "$token_path"
unset bot_token
trap - EXIT
printf 'Installed private token file: %s\n' "$token_path"
