# Claude developer toolchain and consultations

Source candidate; installed acceptance pending. Contract and rationale:
[REQ-SEC-008](../product/ACCOUNTS_CONTROL_AND_SECURITY.md),
[ADR 0078](../decisions/0078-claude-developer-tools-and-consultations.md).

## Explicit local configuration

Optional `claude_file_permissions.toolchain` requires `profile: "developer"`.
Omission preserves existing mounts/PATH; editor and text-only do not gain tools.
Paths below are fictional; prepare the bundle and dependencies before activation.
With a toolchain, control executables must be native binaries or use an absolute
interpreter inside immutable runtime mounts. An `env` shebang is refused before
launch; do not let project Node select the Claude/control interpreter.

```json
{
  "roots": ["/opt/example-tools"],
  "path": ["/opt/example-tools/bin"],
  "git_hooks_path": "/opt/example-tools/hooks",
  "git_python": "/opt/example-tools/bin/python3",
  "author_policy": "/opt/example-tools/author-policy",
  "git_identity": ["Example Author", "author@example.com"]
}
```

Mount sources must be canonical directories with root/worker ownership, no
group/other writes, hardlinks, special files or nested filesystems. They must not
overlap existing runtime/code mounts, private authority, project or session HOME.
Do not mount a generic host HOME or credentials directory. Bundle scripts and
their interpreters/libraries must all be visible. A Python executable alone is
not a Python environment; Node alone is not npm. Provide the actual locked
project dependencies, compiler/build utilities and hook dependencies.

Prepare python/python3 aliases in the dedicated bin directory and the matching
Node/npm prefix read-only. Keep the control Python/hook immutable. Use copy mode
for uv/venv dependencies and avoid pnpm hardlinks. The bounded tree scan remains;
measure real dependency trees instead of disabling it. Required project tests
that cannot run under a nested namespace must be reported and run in CI/host.
Place the venv's own `bin` before generic tool aliases in PATH. A symlink to a
venv Python from another `bin` can select the system prefix and lose its
dependencies; check `sys.prefix` and a required dependency import, not just
`python3 --version`.

Each confined Claude turn receives a compact environment note from its actual prepared
profile/toolchain: available native tools, project instructions, configured
Git/hook Python, Git/hook limits, human command decisions, consultations and unknown
outcomes. It contains no host credentials or session HOME. Confined launches
set `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`, intended to prevent native Bash from
moving a long test/build/hook into an unsupported background task. Confirm the
native effect in the installed canary. Keep explicit
command timeouts; task/child stream guards remain enforced.
Native foreground Bash heartbeats refer to the executing tool as their parent.
The developer stream policy recognizes only their exact shape and an active Bash
ID observed in the same reader; tool completion retires that binding. These
progress records do not grant tools or forward command output.

PATH, validation Python and author policy must resolve inside readonly tools or
runtime mounts, never the writable project. Git hooks may live in the project.
PATH command symlinks must also resolve inside readonly tool/runtime mounts.
The mount is the readonly boundary; the author-policy file itself must still be a
single-link regular file owned by the worker with mode `0600`, or the history scan
ignores it and reports the pinned merge identities. Each PATH command must be the
intended entry point: a venv `bin` placed first can shadow a configured wrapper
with its own console script of the same name, such as an unconfigured
`hub-consult`. Check `command -v` for each tool inside the namespace.
The shipped hooks accept
`HUB_PUBLISH_PYTHON` and `HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE` from trusted launch
configuration before falling back to host local settings. They retain the staged
tree, privacy/history and canonical gates. Refresh installed hook sets through
the existing installer after a hook-source change; never use `--no-verify`.
Launch-local Git settings do not edit shared/sibling configuration. Explicit
upstream/config writes remain unavailable in linked worktrees; create a local
branch without implicit tracking. Explicit `--track` or `push -u` can change files
or remote state before failing on readonly config: inspect status, diff and HEAD,
preserve changes and do not repeat/reset blindly. An independent registered clone is the simple
fallback when repository administration is required.

## Text-only consultation

Prepare a private readonly configuration and an inference-only client key that
has no signing, management or host-control authority. Verify its actual CPA
upstream route and absence of paid/API/extra-usage fallback before setting the
declaration true. Examples deliberately use nonexistent model IDs.

```json
{
  "endpoint": "http://127.0.0.1:19001/v1/responses",
  "credential_file": "/opt/example-tools/inference-key",
  "subscription_route_verified": false,
  "routes": {
    "astra": {"model": "example/astra", "response_model": "example-astra", "effort": "high"},
    "sol": {"model": "example/sol", "response_model": "example-sol", "effort": "high"}
  },
  "timeout_seconds": 300,
  "max_output_tokens": 32768
}
```

Config and key must be private regular files, owned and readable by the worker, with no
symlinks/hardlinks or group/other permissions. Route names/models are explicit;
the CLI does not discover them or substitute another route. `response_model`
must match the real response identity, which can differ from the requested alias.
The declaration is an activation prerequisite, not billing proof. Bash can read
this credential: limits/allowlists in this client do not prevent direct use of
the inference endpoint by trusted project code.

```bash
hub-consult --config /opt/example-tools/consult.json astra < question.md
```

When the package entry point is not available in the namespace, prepare a fixed
wrapper using the visible Python (with the locked aiohttp dependency) and immutable
`consult.py` plus this exact config.
Only stdin is context; no repo/transcript/environment collection occurs. This is
visible advice, not an agent that investigates or writes the project. Source
review still follows [the Opus runbook](CLAUDE_SOURCE_REVIEW.md).

One invocation performs at most one POST. It returns 0 only for complete visible
text and a matched model; 1 denotes local refusal, and 2 a failed or
unknown/incomplete request. None authorizes automatic retry. The command never
logs raw HTTP errors, key or reasoning. Stop terminates the owned caller with its
parent; remote completion/usage can remain unknown. Output limits include internal
reasoning; a limit can be reached without visible text. See
[official OpenAI Responses reasoning guidance](https://developers.openai.com/api/docs/guides/reasoning/).

## Acceptance and activation

Focused source checks:

```bash
PYTHONPATH=src:. python scripts/validate.py --profile focused \
  tests.test_namespace_toolchain tests.test_consult tests.test_consult_transport \
  tests.test_claude_permissions_config tests.test_claude_developer_hosted_launch \
  tests.test_publish_preflight tests.test_namespace_worktrees
```

The required namespace CI runs the real toolchain fixture with Node 22.
The full canonical suite includes `tests.test_consult_transport`. Nested temporary
Git fixtures must clear outer `GIT_AUTHOR_*`/`GIT_COMMITTER_*` identity and author
policy before testing their own configuration.
Offline results are separate from productive Claude/native/Telegram evidence. For the
installed canary, have Claude create a branch, edit, run real project tests/build,
commit with the actual required hook, request one configured consultation and
resume the exact session. Check Deny, stop, grant reset, private-path absence and
preserved other worktrees. A miniature fixture is not a successful Hub/tlive build.
Push/PR needs separate repo-scoped Git/gh authorization; commit does not prove it.

Prepare exact clean candidate/compatible rollback, consistent backup, runtime
bundle/config and recovery-catalog update. Fresh idle/uncertainty checks and a
separate owner deployment authorization precede activation. Update only the idle
Claude worker, then the matching attach-only tlive when carrying ADR 0077; rollback
tlive before worker. Preserve independent Codex and unrelated components. Use the
[restart runbook](HUB_RESTART.ru.md) and independent outcome notification. Never
replay a consumed installer or restore history to undo runtime changes.
