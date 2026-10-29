# ADR 0082: Claude workers may use the subscription directly

Status: owner direction 2026-10-29; source implementation. Architecture
consultation, independent review and installed acceptance pending.
Date: 2026-10-29
Owner: repository owner. Implementation owner: Hub maintainer.
Contract: [REQ-AUTH-009](../product/ACCOUNTS_CONTROL_AND_SECURITY.md).
Extends [ADR 0039](0039-claude-cpa-worker-scaffold.md); keeps the trust model of
[ADR 0072](0072-trusted-personal-installation.md) and
[ADR 0081](0081-claude-workstation-host-view.md).

## Context

Claude workers reach the subscription only through a loopback CPA route. CPA
stores the account's claude.ai OAuth credentials and sends requests on the
CLI's behalf. Anthropic's
[Claude Code legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance)
reserves subscription OAuth for Claude Code and native applications, says that
credentials and session tokens must not be collected, stored or intermediated,
and says that Pro and Max limits assume ordinary individual use. The same
[authentication documentation](https://code.claude.com/docs/en/authentication)
documents `claude setup-token`: a one-year subscription token for scripts and
CI, read from `CLAUDE_CODE_OAUTH_TOKEN` by the unmodified CLI, which can only
make model requests. The owner first chose one account used directly for every
project. Upgrading that account to a larger plan then proved impossible, and the
owner chose two accounts instead, each permanently bound to its own project.

## Decision

1. The Claude agent gets an explicit `auth_route`: `cpa`, the unchanged default,
   or `subscription`. Nothing is inferred from the environment.
2. The `subscription` route requires a non-empty `CLAUDE_CODE_OAUTH_TOKEN` and
   refuses `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY` and
   every cloud-provider selector before a process starts. In Claude Code's
   precedence a bearer token or API key outranks the subscription token, and
   headless mode uses an API key without asking, so either would silently change
   the billing route. A settings `apiKeyHelper` also outranks the token:
   file-tool launches disable settings sources, and installed acceptance must
   show that the worker's own Claude settings define none for text-only turns.
   `--bare` is not used, since it ignores the token.
3. The `cpa` route additionally refuses a present subscription token, keeping
   exactly one credential source.
4. Text-only, `editor` and `workstation` turns may use the route. Without Bash
   the model cannot read its environment. `workstation` already shows the whole
   host, including the CPA credential store and the token file, which ADR 0072
   and ADR 0081 accept; the token in its environment adds no new exposure class.
   `developer` hides private paths, so a token readable by its Bash would be
   new exposure. Configuration loading and the launch refuse that combination.
   Claude Code's `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` is not adopted: it needs a
   nested bubblewrap that `developer` disables and it changes Bash isolation.
5. The owner creates each token and writes it to a private 0600 environment
   file read only by the Claude worker units. Agents never handle it; it never
   enters `hub.json`, Git, logs or Telegram.
6. Before activation the owner confirms that the account's extra usage is off.
   This is the subscription form of the no-paid-fallback rule; configuration
   alone does not prove the billing route.
7. A project may be bound to its own fixed account:
   `subscription_token_by_project` maps a registered project to an environment
   variable named `CLAUDE_CODE_OAUTH_TOKEN_<SUFFIX>`, and every other project
   uses `CLAUDE_CODE_OAUTH_TOKEN`. The launch passes the selected token as
   `CLAUDE_CODE_OAUTH_TOKEN` and removes every per-project variable, so a turn
   sees exactly one account. A bound project without its token, or a turn
   without a project, fails instead of falling back to another account. A
   fixed binding keeps each project's work, sessions and prompt cache on one
   account. Account pools, rotation and limit-driven switching remain rejected:
   they work against the ordinary individual use the plans assume. Anthropic's
   consumer terms do not forbid one person holding several accounts, but they
   do not grant it either; the owner accepts that Anthropic may treat linked
   accounts together. A larger plan stays the preferred capacity path once it
   can be bought.
8. CPA remains for GPT consultations and cross-provider review. Moving the
   Claude reviewer route and showing Claude limits in `/accounts` from passive
   CLI output are follow-up work.

## Consequences

Rollback is `auth_route: cpa` and a Claude worker restart; nothing persisted
depends on the route. The token expires after a year and must be renewed by the
owner. Installed acceptance must show a text-only and an editor turn on the
subscription, an injected API key refused before launch, no `apiKeyHelper` in
the worker's Claude settings and the extra-usage setting off on every account,
and, when projects are bound, one turn per bound account, all tied to the exact
deployed revision.
