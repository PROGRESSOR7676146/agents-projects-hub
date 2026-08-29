# Security Policy

## Supported versions

Security fixes are applied to the latest tagged release and the `main` branch.
The project is still a pilot; older snapshots are not maintained.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability, leaked credential, or
cross-project routing failure. Use GitHub's **Security → Report a vulnerability**
private reporting flow for this repository. If private reporting is unavailable,
contact the repository owner privately and include only the minimum reproduction
needed to establish impact.

Please describe:

- affected version or commit;
- whether the issue crosses a project, topic, agent, or approval boundary;
- a safe reproduction using fake credentials and a disposable repository;
- any known exposure of tokens, hidden reasoning, terminal contents, or paths.

Never attach real bot tokens, Codex credentials, state databases, or private
conversation transcripts.

## Operational response

For a suspected live compromise:

1. Stop the user service locally.
2. Revoke affected Telegram/provider credentials.
3. Preserve the private state database for investigation; do not publish it.
4. Do not resume pending turns or approvals.
5. Rebind topics and sessions only after verifying canonical project roots.

The detailed trust boundaries and fail-closed requirements are documented in
[`docs/SECURITY.ru.md`](docs/SECURITY.ru.md).
