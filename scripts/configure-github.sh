#!/usr/bin/env bash
set -euo pipefail

repository="${1:-}"
if [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
  printf 'usage: %s OWNER/REPOSITORY\n' "$0" >&2
  exit 2
fi
if ! gh auth status --hostname github.com >/dev/null 2>&1; then
  printf 'GitHub CLI is not authenticated. Run: gh auth login --hostname github.com\n' >&2
  exit 2
fi

gh api --method PUT "repos/$repository/vulnerability-alerts" >/dev/null
gh api --method PUT "repos/$repository/automated-security-fixes" >/dev/null
gh api --method PUT "repos/$repository/private-vulnerability-reporting" >/dev/null

rules_file="$(mktemp)"
trap 'rm -f "$rules_file"' EXIT
cat >"$rules_file" <<'JSON'
{
  "name": "Protect main",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": ["~DEFAULT_BRANCH"],
      "exclude": []
    }
  },
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {
      "type": "pull_request",
      "parameters": {
        "dismiss_stale_reviews_on_push": false,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_approving_review_count": 0,
        "required_review_thread_resolution": true
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "do_not_enforce_on_create": true,
        "required_status_checks": [
          {"context": "test (3.11)"},
          {"context": "test (3.12)"},
          {"context": "test (3.13)"}
        ]
      }
    }
  ],
  "bypass_actors": []
}
JSON

existing="$(gh api "repos/$repository/rulesets" --jq '.[] | select(.name == "Protect main") | .id' | head -n1)"
if [[ -n "$existing" ]]; then
  gh api --method PUT "repos/$repository/rulesets/$existing" --input "$rules_file" >/dev/null
else
  gh api --method POST "repos/$repository/rulesets" --input "$rules_file" >/dev/null
fi

printf 'Configured vulnerability reporting, dependency security updates, and main ruleset for %s\n' "$repository"
