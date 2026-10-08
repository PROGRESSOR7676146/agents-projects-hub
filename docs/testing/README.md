# Testing and acceptance strategy

## Development loop and canonical acceptance

During development, run cheap repository contracts and explicitly selected tests:

```bash
python scripts/validate.py --profile focused tests.test_documentation_contract
```

With no test selectors, `--profile focused` runs only static preflight checks.
It omits Git history scanning, whole-project Pyright and full test discovery: its output explicitly
says **not canonical acceptance**. Select the tests affected by the change;
the profile does not guess test coverage from a diff.

The full canonical command remains:

```bash
python scripts/validate.py
```

Both profiles run documentation/release metadata contracts, publishable
configuration, release-lock verification, formatting, Ruff, and tree privacy.
Canonical additionally scans all reachable Git history before running
whole-project Pyright and all unit/integration tests. The full privacy/history
scan remains mandatory before every commit, even after focused checks pass.
Each stage reports elapsed time and stops on failure; exit 0 means the selected
profile passed, 1 means a failed/unavailable stage, and 2 means invalid arguments.
Test selectors are accepted only in the focused profile; they import sibling
fixtures from `tests/` exactly as discovery does. The canonical test stage asks
discovery for every test module, including nested test packages, then runs
each in its own process, several in parallel (CPU count up to eight; override
with `--jobs N`). Each test runs in the module discovery found it in, the tests
that start must be exactly the discovered ones by test id, and any module
under `tests` that defines `load_tests` is refused, because the suite such a
hook builds cannot be reproduced by an isolated module run. It names every
failing, empty, mismatched or timed-out module after all modules finish, so
each module must pass on its own. See [ADR 0041](../decisions/0041-parallel-isolated-test-modules.md).
Automated tests
use fake transports and temporary Git/SQLite fixtures. They must not contact
real Telegram groups or consume provider tokens.

Codex transport pressure tests use scripted pipes/WebSockets and an owned
fictional Python JSONL peer. More than 1,024 foreign notifications must not
prevent an RPC response; early final/completion/context and rolling quota
updates must survive, and an interleaved approval must be declined on stdio
without an automatic grant. Cover a successful client left idle before its
next turn, bounded multibyte and unterminated frames, ordered drain before
terminal failure, close during producer/consumer waits and delivery after a
receive timeout. These fixtures establish source transport behavior, not the
origin of a deployed subscription or native/Telegram live acceptance.

`tests.test_codex_stdio_failure_drain` and the stdio pressure tests additionally
cover writer failure with a full inbound queue and unread pipe tail, delayed
tail after an empty receive timeout, both first-cause orders and a synchronized
race, and close while the reader is full or empty. A controlled writer proves
that a decline can enter the outbound queue before failing; its final/context/quota
tail still reaches the exact completion checkpoint with the Hub notice and
unchanged raw visible items. EOF before a buffered approval covers terminal
reply-admission refusal; ordinary healthy EOF produces no notice.

Fault-drain regressions require saved accepted identity and explicit completion,
bound foreign traffic and an already-blocked quiet reader, preserve the original
quiet deadline while polling, and retain text/notice across storage, permission,
EOF and completion-boundary expiry failures. They prove no second submission or
grant, not delivery of an admitted decline. Test-only shortened poll/window
values make the quiet fault test bounded; production values and limitations
are described in [queue recovery](../operations/QUEUE_RECOVERY.md).

`tests.test_codex_rpc_deadlines` uses module-local fake clocks with real Hub
clients. It covers more than 1,024 foreign frames exhausting a fixed default
response deadline, explicit/quiet budgets, late responses/rejections/approvals,
send time, managed metadata refusal, early approval visibility and actual
external/embedded post-submission uncertainty with retained root exclusion and
one submission. It does not establish a whole preparation wall-clock budget or
legacy inline recovery parity.
`tests.test_preparation_deadline_retry` exercises real flooded RPC preparation
expiry through both workers, exact delivered-notice retry with the original
payload/session generation and zero productive submission. Typed expiry under
the exact preparation boundary is distinct from same-text generic errors,
policy refusals, contradictory evidence and post-submission uncertainty; the
latter retain zero retry tickets and the existing root exclusion.

Notification/preparation recovery acceptance must cover two independent roots
and worker connections: more than 1,024 foreign notifications during preparation
of the second turn must not abort it before start. Preserve both exact results,
final events, approvals and visible telemetry. Repeat after a successful client
has retained a previous thread subscription. Separately inject two consecutive
preparation failures, Reply to each exact delivered notice and verify that only
one eventual native turn receives the original authorized task/context, with
truthful retry controls and no reused tool grants. Include stop/contradiction,
materials, queued binding drift, transaction faults, restart deduplication and
earlier holds. Offline native fixtures use deterministic local protocol stubs;
their evidence does not close separately authorized live Telegram acceptance.

`tests.test_preexecution_retry_runtime` reconfigures a queued Codex alias retry
to OpenCode or Antigravity in each queue consumer. Dispatch and material
preparation must remain untouched; the saved payload and truthful pre-execution
notice survive without another retry ticket or execution checkpoint.

Claude process-observation fixtures (`tests.test_claude_process_observation`,
`tests.test_claude_activity`, `tests.test_claude_activity_lifecycle` and
`tests.test_claude_activity_migration`) use owned fictional processes and private
temporary state. They prove post-Popen worker callback propagation, optional
open/retire/evaluator failure isolation, exact first-send fencing, committed
visible-message races, permission pending/roundtrip/expiry/revocation/bounds,
retirement and restart episodes, attempted/unknown/rejected send preservation,
and additive schema-42 backup/DDL rollback. Buffered runners do not establish
process-start evidence. These checks do not establish native turn acceptance,
tool timing, subscription routing or deployed Telegram behavior. The owning
subset and limits are [REQ-QUEUE-012](../product/DURABLE_QUEUE_AND_CONTROL.md).

Delivery-certainty fixtures (`tests.test_delivery_certainty`,
`tests.test_progress_certainty`, `tests.test_delivery_certainty_migration` and
`tests.test_observed_delivery_preservation`) exercise SQLite fences and fictional
Telegram receipts. They cover malformed/bool IDs, external/embedded/document
paths, post-HTTP expiry, receipt-commit faults, prefix preservation, cleanup,
pre/post-fence recovery, populated schema42 backups and trigger/FK/DDL rollback.
Late exact native proof preserves uncertain notice parts and old referenced
artifacts without a substitute send. A sending notice defers reconciliation until
receipt, proven rejection or unknown recovery settles it; normal replacement
archives every original part atomically, including after a receipted prefix.
Opener-level form/multipart rejection tests cover real HTTP400/403, malformed or
conflicting bodies, HTTP408/5xx and incomplete HTTP200 responses. Unknown-head
fixtures prove same-topic execution/delivery remain blocked while another topic
can deliver. The explicit reconciliation deployment gate remains open.
Schema44 delivery-hold fixtures (`tests.test_delivery_hold` and
`tests.test_delivery_hold_migration`) additionally cover full-manifest stale
tokens beyond 64 parts, receipt provenance, competing connections, immutable
exact retries, transaction faults, both released FIFO barriers and retained
native/writer/owner-hold/stop boundaries. Preview/apply use only fictional local
state; missing/older schemas are refused without migration or credentials.
Populated43-to44 backups and DDL faults preserve all existing evidence.
No live inference, Telegram or service
change occurs. This is offline source/fault evidence, not deployment acceptance.

Publication sequence with the installed hooks: focused checks → commit (the
pre-commit gate) → push (one full canonical run on the clean commit) →
independent exact-revision CI/CodeQL. Do not run the same full validator
manually immediately before this push. Without the hooks, run the
privacy/history scan before every commit and the canonical command on the final
clean commit before publication. A failed gate blocks publication; fix it,
rerun the affected checks, and commit before retrying. There is no validation
receipt/cache: another push runs the full hook again. The trade-off is recorded
in [ADR 0034](../decisions/0034-fail-fast-maintenance-validation.md).

The pre-commit gate (`--profile commit`) runs every cheap contract, the
privacy/history scan and the complete parallel test suite; only whole-project
Pyright is left to the pre-push canonical run and CI. It takes about a minute
and a half. It refuses a commit while unstaged or untracked, non-ignored files
exist, because it validates the working tree. A checkout that predates the
profile falls back to focused checks plus the history scan. The rationale is
recorded in [ADR 0042](../decisions/0042-pre-commit-gate.md).

GitHub CI and tag-release validation both call the same reusable
`.github/workflows/validate.yml` matrix for Python 3.11, 3.12, and 3.13. Each
matrix entry installs the exact `uv.lock` dependencies of the `dev` extra with
`uv sync --locked`, including the test-only Telegram client used by the
acceptance-actor unit tests, runs the suite once more in a single shared process
to catch cross-module state leaks, prepares an optional external public-author
declaration from a repository Actions variable, and runs this full canonical
command. The value enters through the step environment and is written without
output to a temporary `0600` file outside the checkout. It is not a secret or a
security boundary against candidate code running as the same runner user.
When the variable is absent, including in a fork or reusable-workflow context
where it is unavailable, no declaration is set and the required canonical
privacy scan runs without an exception; history that needs it remains red. The
release publication job depends on the entire reusable validation job and alone
has `contents: write`. `tests/test_workflows.py` parses the workflows
structurally, including GitHub's `on` key, and has negative temporary-copy cases
for a missing dependency, bypass condition, or Python 3.13. That offline
contract test rejects skipped/error-tolerant validation as well. A temporary
Git fixture executes the revision guard with matching and mismatched event,
checkout and annotated-tag commits without publishing a release. These tests
prove local wiring and guard behavior; a successful hosted Actions run remains
separate evidence.

A separate, non-required CI job publishes branch coverage per module in the
run summary as a trend signal, not a gate. Locally, after installing the dev
extra: `python -m coverage run -m unittest discover -s tests -q` followed by
`python -m coverage report`. Code executed in test subprocesses is not counted.

`tests/test_fault_injection_matrix.py` is the subprocess queue acceptance gate.
It uses marker-synchronized fictional child actors, bounded parent waits, and
forced process termination to join real Controller polling, SQLite recovery,
isolated workers, and the standalone sender. Lower-level state-machine tests
remain in their focused modules.

## Codex preparation conservation regression

The preparation conservation regression is
`tests.test_codex_notification_conservation`. Two independent workers and roots
join the real client, execution journal and durable outbox against scripted
transports. The second receives 3,600 foreign events while the first remains
accepted and active. Separate start/resume cases check both saved finals,
early/late approval resolution, context and account quota observations. A control
case models the older unfiltered retention rule and reproduces its bounded
preparation failure. It is offline conservation evidence, without native
broadcast attribution, live approvals, services, Telegram or inference.

## Privacy gate

`python -m hermes_codex_router.privacy_scan . --history` scans both the proposed
tree and every reachable Git blob plus commit/tag metadata. It rejects:

- files under `docs/history/` or `docs/handoffs/`;
- non-example email addresses, home paths, bot usernames, Telegram chat IDs,
  private invites, and bot tokens;
- raw agent/session transcript markers;
- local configuration, database, key, socket, session, and log files;
- private deployment fingerprints retained only as one-way hashes.

False positives must be resolved by using conspicuously fictional fixtures, not
by allowlisting real deployment data. The sole external policy declaration
permits one exact public author email only in its original author-email byte span
after strict merge structure and pinned GitHub signature verification. It does
not suppress body, trailer, file, credential, path, invite or other rule matches.
The fingerprint rule alone may be suppressed on the complete author-name or
hosted source-owner byte span when it exactly equals the valid local origin
owner; case variants, substrings, whitespace, lookalikes and all other display
names remain scanned. The scanner accepts zero or one final LF in the declaration
without stripping, case-folding or Unicode normalization; an absent, malformed,
linked, non-owner, non-`0600`, oversized or in-checkout file gives no exception.
Regression coverage creates a real temporary Git replacement mapping and proves
that history reads keep the original object bytes; the scanner also recomputes
each SHA-1 object ID before any metadata exception is applied.
An isolated canonical-history fixture combines a fictional declaration with
real temporary Git objects and a mocked successful signature result to test
policy wiring only. Pinned-key cryptographic verification has separate tests;
the mock is not evidence that a fixture was cryptographically signed.

## Optional offline native Codex profile rehearsal

`tests.test_codex_native_profiles` exercises an explicitly supplied native Codex
binary against a deterministic local Responses fixture. It requires Linux,
system bubblewrap and usable network/PID/IPC namespaces. Supply the native executable,
not a shell wrapper or the ordinary logged-in CLI configuration:

```bash
HUB_NATIVE_CODEX_FIXTURE_EXECUTABLE=/home/example/tools/codex-native \
  HUB_REQUIRE_NATIVE_CODEX_PROFILE_TESTS=1 \
  HUB_REQUIRE_NAMESPACE_TESTS=1 \
  PYTHONPATH=src python -m unittest -v \
  tests.test_codex_native_profiles tests.test_codex_native_mcp \
  tests.test_codex_native_namespace
```

The binary is copied into a disposable empty-root namespace with read-only
system runtime, fixture scripts and managed requirements. Real Codex authentication and
configuration are hidden, provider credential variables are excluded, and external
network access is unavailable. A temporary managed requirements file and fictional
project/key/symlink controls establish the policy under test. The fixture permits
at most four local Responses requests per case. Command and file approvals are
declined; permission requests receive an empty turn-scoped grant. MCP elicitations
are declined unless the test explicitly
arms one exact synthetic fixture consent; the fixture requests no persisted grant. This is not a
human approval host or approval transport acceptance.
It controls only its owned disposable processes, never installed services.
Host-side repairs and observations reject symlinks at every project path
component. The unarmed MCP case also verifies the absence of probe mutations
independently of the native item's reported failure.

The native cases cover explicit profile selection, exact stored-thread resume
after restarting the disposable app-server, actual tool access denials, legacy
turn policy replacement, separate standalone command overrides, and the limits
of profile metadata. A native final message alone is insufficient: denial evidence
requires one completed successful command from the exact current thread and turn,
project read/write controls, and the complete boolean probe payload. Old resumed
output, duplicate commands, failed execution and partial payloads are rejected.

The fixed MCP server exposes only a nonce-only fictional probe. Direct RPC must
use zero Responses requests; it is diagnostic evidence without a native turn.
Current-turn evidence requires one completed MCP item matching the exact
thread, turn, server, tool, arguments and fresh nonce. Both structured and textual
versioned probe payloads must agree; duplicate JSON fields, malformed booleans,
old items and assistant completion alone are rejected. Explicit synthetic consent
is one-use and bound to that current item; restart and terminality clear it.
Each armed native turn must record exactly one matching fresh consent acceptance,
including after restart, before its successful output counts as exposure evidence.

With native Codex 0.159.2, the fixed command descendant retained positive project
read/write controls while denying fictional authority reads/writes and Git writes.
Direct MCP RPC exposed the fictional authority to both the MCP process and its
fixed Python child. A current-turn MCP call was denied by default; after one
explicitly armed synthetic fixture consent, it showed the same exposure,
including after exact-thread restart/resume with a fresh nonce. These results
demonstrate a custody gap for this tested MCP launch path. They do not establish
human approval delivery, arbitrary helper isolation, deployed custody or Telegram
acceptance. The server validates visible fictional authority controls at startup;
a future launch that hides those controls may fail startup and requires separate
investigation, rather than being counted as a successful denial probe.

The corpus was rehearsed with native Codex 0.159.2. No binary is downloaded by
the tests or bundled with the repository. With no executable supplied, the
native cases explicitly skip; requiring them makes that absence fail. Once opted
in, fixture/protocol/namespace failures fail the run. The ordinary canonical suite
still exercises the evidence parser without starting Codex. Skips and parser
tests are not native evidence, and this offline corpus neither implements managed
profile support in Hub nor establishes deployed custody or Telegram acceptance.
The required CI namespace job also runs the outer wrapper witness without a
Codex binary, proving absent source aliases and unwritable mounted scripts.

## Optional offline native Claude transport

`tests.test_claude_native_transport` uses an explicitly supplied standalone ELF
Claude executable against a fictional HTTP endpoint inside a disposable empty-root
network/PID/IPC namespace. It copies the bounded binary and supplies only readonly
system runtime, the fixture actor and the production
stream parser. Host HOME, project, credentials and service endpoints are absent.

```bash
HUB_NATIVE_CLAUDE_FIXTURE_EXECUTABLE=/home/example/tools/claude-native \
  HUB_NATIVE_CLAUDE_FIXTURE_SHA256='<locally-verified-sha256>' \
  HUB_NATIVE_CLAUDE_FIXTURE_VERSION='2.1.285 (Claude Code)' \
  HUB_REQUIRE_NATIVE_CLAUDE_TRANSPORT_TESTS=1 \
  PYTHONPATH=src:. python -m unittest -v tests.test_claude_native_transport
```

The bearer/API-key × SSE-success/HTTP-529 matrix derives its base argv from the
production text-only builder, without bare mode. A canonical regression checks
every production start argument and setting, including a caller-chosen fictional
session UUID checked by both reader and parser. Saved-session resume is separate.
Shared native settings arrive through this host-built argv. Fixture-only additions empty setting
sources/MCP, disable session persistence and model switching/fallback, limit the
turn to one, and replace the system prompt. Fixture-only environment settings
suppress retries and limit output tokens. Restricted mode already loads only
managed and explicit settings according to the CLI contract; the extra empty
source flag is a fixture bound. Production retry/fallback behavior is not proven
by this corpus. The fixture requires the chosen model/effort,
empty tools, 1,024 output tokens, exactly one served Messages POST, at most one
optional HEAD and no unknown requests, empty connections, timeouts or retry POST.
Host-side file reads and an actual loopback exchange establish positive controls;
the namespace actor must prove both targets unreachable. Native stdout is bounded
and passed through the real stream reader/parser; evidence contains only fixed
categories, booleans and counts, plus validated CLI version, copied binary SHA256
and parser Python version. Required native runs pin the expected digest and
version from private locally verified evidence; never put real binary identities
in Git. Success requires exactly one visible message; HTTP 529 requires the native
`success`/`is_error:true`/exact-529/string-result envelope, an overloaded failure,
and zero visible assistant messages with an error-bearing latest assistant event.
The actor requires Python 3.11 or later; the host validates the reported Python
version and every terminal diagnostic key/value before printing the evidence.
Special-file sources refuse before open and regular files use nonblocking open;
timeout, output-bound, callback failure and parent-exits-first regressions check
owned group termination before leader reaping and pipe cleanup.

The corpus was rehearsed with CLI 2.1.285. It discovered that built-in mods are
independent of safe mode; shared settings now disable the four known optional
mods through documented `enabledPlugins`. The mandatory policy security mod is
not disabled, and unknown/enabled plugin metadata still fails the strict guard.
Disabling participation does not prove that the binary never imports/registers
bundled modules. The native success result variant may also carry `is_error:true`:
an exact integer 4xx/5xx status and string result prove only a terminated failed
turn. Missing/malformed status/result, conflicting success, duplicate terminal,
session drift and trailing events stay ambiguous. HTTP 529 is classified from
that exact native status, without reading diagnostic text.

No executable is downloaded or bundled. Without explicit opt-in, native cases
skip; requiring them makes absence fail. Once opted in, namespace, protocol and
compatibility failures fail without fallback. Ordinary canonical tests exercise
the fixture evidence and failure cleanup without launching Claude. A fictional
API-key path is compatibility evidence, not a paid-route authorization. No
real account, CPA upstream, subscription billing, live provider inference,
installed service, Telegram or human approval is exercised. Native file-tool
permissions, saved-session/local transfer and productive custody remain separate.

Primary references: [built-in mods](https://code.claude.com/docs/en/plugins/mods/overview#mods-built-into-claude-code),
[plugin settings](https://code.claude.com/docs/en/settings-reference#enabledplugins)
and [SDK result message](https://code.claude.com/docs/en/agent-sdk/typescript#sdkresultmessage).

## Offline review pipe primitives

`tests.test_review_bridge_protocol` and `tests.test_review_bridge_attempt` exercise
the [bounded pipe primitives](../decisions/0058-bounded-review-pipe-primitives.md)
without a provider, socket, process launch, environment credentials or live route:

```bash
PYTHONPATH=src:. python scripts/validate.py --profile focused \
  tests.test_review_bridge_protocol tests.test_review_bridge_attempt \
  tests.test_review_bridge_sequence tests.test_review_bridge_write_buffer \
  tests.test_review_materials
```

Codec tests cover fragmented/combined frames, declared payload limits before body
accumulation, aggregate byte/frame budgets including headers and empty frames,
invalid caller types and permanent retirement after failure/EOF. Gate tests use
real sealed capsules and a recording fake callback: exact material binding and
trusted request bytes, source changes after sealing, unsealed/closed material,
forbidden frame directions, single-use concurrent/reentrant admission, safe
callback diagnostics and cancellation/deadlines before and after claim. The host
injected callback has no live upstream implementation; its return is not provider
acceptance or completion. Deadline checks cannot interrupt that callback.

Sequence and write-buffer tests add wrong direction/order, duplicate controls,
stdout before request, graceful cancellation with bounded discarded in-flight
stdout, early exit versus incomplete response, EOF in every unfinished phase,
per-direction/stream budgets, partial writes and zero-progress would-block,
backpressure without cumulative admission, one stable outstanding offer and
abort midway through headers/payloads. A serialized fake owner retries buffer
admission without advancing sequence twice. A late sequence failure preserves
one consumed fake callback and refuses resubmission. Buffer abort never permits
appending CANCEL to a truncated frame; after buffer abort or failure the future
I/O owner must close the pipe unconditionally. Uninitialized frame objects retire
both sequence and buffer through fixed diagnostics without exception chains.

These are in-process fixtures. They do not prove an actual namespace pipe,
HTTP/native compatibility, durable workflow deduplication, role authorization,
subscription/no-paid-fallback, deployed custody or Telegram acceptance. The
ordinary suite must never create a real inference route. Owned nonblocking I/O,
deadline enforcement, process cleanup and the namespace/native witness remain
next steps; the partial-write buffer itself performs no physical writes.

## Live acceptance boundary

The reusable validation workflow also runs a required Ubuntu 24.04 namespace
job with system bubblewrap and Python 3.12. `HUB_REQUIRE_NAMESPACE_TESTS=1`
turns missing fixtures or unavailable user namespaces into failures. It runs
real fd-bind isolation and a fictional socket/receipt-journal Allow/Deny
roundtrip, plus an authority-alias/privilege/shared-network rehearsal with
fictional sentinels, with no model or Telegram calls. The third scenario
deliberately demonstrates loopback and abstract-socket descriptor exposure;
see the [custody runbook](../operations/CLAUDE_CUSTODY.md#automated-offline-rehearsal).
The same strict job runs `tests.test_process_namespace_rehearsal`, a second
provider-free consumer of the [neutral namespace core](../decisions/0056-provider-neutral-process-namespace.md).
Its Python parent and exec child read authorized project material, fail project
and Git writes, retain a separate writable session HOME, and cannot reach
fictional host TCP, pathname or abstract sockets. Host controls prove those
targets work, including a real synthetic `SCM_RIGHTS` transfer. An accessible
process FD-table census checks authority and mount-pin inode identities rather
than reusable descriptor numbers. This is kernel/fixture evidence, with no
provider, Telegram, live service or advisor activation.
`tests.test_review_materials_namespace` also runs in this strict job. It sends
an explicitly selected [sealed text capsule](../decisions/0057-sealed-review-material-capsules.md)
through owned stdin to an isolated parent and exec child, without mounting the
original project or Git. Unit coverage checks exact digest/size, parser bounds,
source replacement/mutation, kernel seals and failure cleanup.
`tests.test_mount_lookup` and `tests.test_claude_mount_pins` cover the shared
lookup guard with scripted flag/geometry evidence, targeted intermediate and
terminal directories, repeated final walks, unavailable metadata, and ordinary
real Unicode paths. They are not a real casefold/cache-state or XFS witness.
All source ancestors must satisfy the supported lookup/read-access boundary in
[ADR 0056](../decisions/0056-provider-neutral-process-namespace.md); overlay
ancestors and tmpfs without working flag queries refuse conservatively. Such
refusals must not be reclassified as absent private paths or ignored by fixtures.
Descriptor assertions track calling-thread open/dup/dup2/pipe and the optional
Python memfd allocator, without closing test resources; untracked snapshot
differences remain diagnostics, not cleanup authority. Allocation-fault tests
also check capsule descriptor closure explicitly.
This is snapshot
and fixture evidence; durable material authorization and productive review remain open.
An executable-specific AppArmor
profile permits bubblewrap user namespaces only on that disposable CI runner.
Developer environments may skip unavailable namespace fixtures; those skips
are not namespace evidence. The source ruleset setup includes this check;
applying a changed remote ruleset remains a separate authorized operation.

Live Telegram acceptance is a separate owner-coordinated operation because it
changes external state and uses real identities/accounts. Store its transcript,
IDs, account hints, screenshots, and service logs outside Git. Public status may
state only the reusable behavior tested and the kind of acceptance required.
The reusable go/no-go sequence and rollback boundary are defined in
[`LIVE_CANARY.md`](../operations/LIVE_CANARY.md).

## Optional offline native notification attribution

The direct-socket fixture uses an explicitly supplied native Codex executable
inside an empty filesystem/network/PID namespace. A local deterministic
Responses server emits the traffic; real authentication, provider endpoints,
installed services and daemon sockets are absent. Its disposable endpoint
storage is mounted only inside that namespace. Both clients connect directly
to a pinned native socket, without an RPC forwarding proxy.

```bash
HUB_NATIVE_CODEX_FIXTURE_EXECUTABLE=/home/example/tools/codex \
  HUB_REQUIRE_NATIVE_CODEX_PROFILE_TESTS=1 HUB_REQUIRE_NAMESPACE_TESTS=1 \
  PYTHONPATH=src:tests python -m unittest -v \
  tests.test_codex_native_notification_origin tests.test_codex_native_completed_connection
```

Attribution distinguishes fresh connection, initialization, passive metadata and
explicit subscription. It requires source traffic above 1,024 events, exact
terminal history, healthy observers and a positive control after a thread
switch. Structural method/phase/count summaries contain no payload text.
The retirement test uses the real Hub client/transport against that same
disposable native listener. After exact completion and saved native output, it
closes only the completed connection. An independently subscribed peer emits
over 1,024 frames while a fresh Hub client prepares a different thread; raw
pre-filter counts and same-connection RPC barriers prove no inherited turn/item
subscription stream. Global `thread/status/changed` can still reach fresh
unrelated connections; the test excludes only that observed broadcast method.
The peer remains healthy, both threads retain exact final output, and a later
fresh client resumes the original identity and completes another turn.
Four local Responses requests account for all scripted productive turns.
The fixture supports one active scripted Responses stream at a time; this is
parallel metadata preparation, not two concurrently streaming native turns.
Hub durable outbox/stop/publication and cleanup-fault ordering have separate
pipeline regressions in `tests.test_codex_result_lifecycle`.

These tests establish behavior of the supplied executable, not a deployed failure's
source or live Telegram acceptance. Missing optional executables may skip;
the required flags turn unavailable native/namespace execution into failure.
The owner-authorized two-worker canary still must verify both productive results,
final events, approvals, visible progress and quota telemetry through delivery,
plus two successive pre-execution failures retaining the exact task through
notice-bound retries. No restart-only recovery counts as a permanent fix.

## Optional offline native approval and control-loss witnesses

The existing disposable native namespace fixture also exercises the real Hub
RPC client against an explicitly supplied Codex binary:

```bash
HUB_NATIVE_CODEX_FIXTURE_EXECUTABLE=/home/example/tools/codex \
  HUB_REQUIRE_NATIVE_CODEX_PROFILE_TESTS=1 HUB_REQUIRE_NAMESPACE_TESTS=1 \
  PYTHONPATH=src:tests python -m unittest -v \
  tests.test_codex_native_approval_sequence tests.test_codex_native_control_loss
```

The approval witness uses 129 sequential requested/resolved pairs in one exact
turn, peak outstanding one, a deny-only synthetic companion and the retained
completed final. A fixed compatibility case uses two approvals plus final,
with a four-request Responses cap; the full sequence uses exactly 130 requests
and a fixed cap of 130. One completed warmup precedes companion subscription
and is accounted separately. Every request, including retries or unexpected
calls, consumes the cap. Native output must identify the exact denied previous
call, and the next response waits for primary resolution. No grants or approval
authority are introduced. Fixture-plan unit tests cover these bounds and
premature, repeated and mismatched output.

The control-loss witness closes only the primary Hub connection while the
native turn remains active. A fresh actual Hub client reads the exact target,
interrupts it once and independently proves it interrupted, with one local
Responses request and no start/resume/steer on that recovery connection.
The schema48 candidate wires the real Hub checkpoint/control journal into this
witness: accepted identity, send-start hash, matched ACK, quiesced sender and
one permanent send are asserted without a hardcoded authorization callback.
State/runtime regressions cover competing live/protective control, late cycles
and replaced claims, native terminality with unknown sender, satellite/alias
identity, withheld raw completion and embedded progress, stop and no-fallback
maintenance wiring.
Both witnesses passed with Codex 0.159.2. These are offline native protocol
observations, with no real auth, remote inference, installed service, Telegram
or human approval. Late-stop maintenance, ingress-loss policy and live
acceptance remain separate gates in the [control-loss runbook](../operations/CODEX_CONTROL_LOSS.md).

## Publication preflight

Repository maintainers can install the versioned pre-commit and pre-push hooks
after creating the external author-policy file used by the canonical privacy scan:

```bash
HUB_PUBLIC_GIT_AUTHOR_EMAIL_FILE=/home/example/.config/agents-projects-hub/public-author-policy \
  PYTHONPATH=src .venv/bin/python -m hermes_codex_router.publish_preflight --install
```

The installation records only the private file path in local Git configuration
and installs `.githooks/pre-commit` and `.githooks/pre-push` as one immutable,
mode-`0700` hook set under the shared Git common directory
(`hub-hooks/sets/<digest>`). Git runs hooks through the `hub-hooks/active`
link, which one atomic rename switches to a new set; the previous set and all
settings stay untouched until the new installation is verified, and a failed
installation returns to them or reports that it could not confirm doing so.
Settings are restored before a newly created link is removed, so Git never
points at a missing hook directory.
Older installations in `hub-managed-hooks` are migrated the same way and the
old directory is removed only after success. It also records the validated
Python executable used for
installation so linked worktrees do not require separate virtual environments.
The configured hook therefore covers every worktree, including a branch that
predates the versioned hook. When per-worktree Git
configuration is enabled, installation updates and verifies each registered
active worktree's effective hook path so a stale override cannot bypass the
shared hook. Git entries explicitly marked `prunable` are ignored because their
worktree directories no longer exist. The installed set is local Git state and
must be refreshed by running `--install` after a hook update.

Before a push, the hook captures the current worktree root and unsets every
repository-local environment variable reported by
`git rev-parse --local-env-vars`. Nested Git commands in the canonical test
suite therefore operate on their explicit temporary repositories rather than
the caller's index, object database or worktree. The hook then requires a clean
checkout and requires every published ref to resolve to the checked-out `HEAD`,
validates the external declaration through the canonical
scanner rules, and compares it exactly with the one named GitHub repository
variable without printing either value. It then prepends the current checkout's
`src` directory to `PYTHONPATH` and runs `python scripts/validate.py`. This
prevents an editable environment from another worktree from silently supplying
the validator implementation. The hook prints the validator's stage timings
and rechecks tree, HEAD, published refs and author policy before permitting the
push. It never reuses a result from a different Python environment or policy.

The hook fails before publication when GitHub or the repository variable is
unavailable. It reduces avoidable hosted-CI retries; it cannot promise that a
remote runner, dependency service, or network will remain available, and Git's
explicit `--no-verify` option can bypass a local hook. Exact-SHA hosted checks
remain the publication evidence. Forks and reusable callers without the
repository variable retain the fail-closed behavior described above.

## Dedicated acceptance user

Telegram bots never receive messages sent by other bots, so a service bot cannot
impersonate the operator for live E2E. An optional MTProto user actor covers the
bounded, non-destructive baseline while remaining restricted by Hub to one exact
canary topic.

Install the optional client and prepare private deployment files outside Git:

```bash
python -m pip install -e '.[e2e]'
agents-projects-hub e2e-validate PRIVATE_ACTOR_CONFIG
agents-projects-hub e2e-login PRIVATE_ACTOR_CONFIG
agents-projects-hub e2e-run PRIVATE_ACTOR_CONFIG
```

Copy `config/acceptance-actor.example.json` only to a private location and set
mode `0600`. Store only the hash value in a sibling file named
`telegram-api-hash`; that file and the generated session must also be mode
`0600`, and the artifact directory must be mode `0700`. Add the same user/chat/topic
triple to the private Hub configuration under `acceptance_actors`. Never commit
the copied config, session, identifiers, or result files.

For the first login only, `expected_user_id` may be omitted. `e2e-login` prints
the authenticated numeric user ID locally; immediately add it to both the actor
config and the matching Hub `acceptance_actors` entry. `e2e-validate` and
`e2e-run` fail closed until that identity is pinned.

Treat the canary topic as exclusive for the duration of a run. The runner fails
fast when it observes in-topic traffic from a sender outside the pinned actor,
Hub identity, and configured provider identities. Re-run only after the topic is
quiet; an interrupted or contaminated artifact is not acceptance evidence.
The runner stops after its first failed check; diagnose and drain that bounded
scenario before starting another run.

The optional `p0_p1_live` check is the disruptive seven-result P0/P1 suite.
Add `state_path` (an absolute mode-`0600` live SQLite file) and
`allow_service_restart: true` only for an owner-declared maintenance window,
align exactly one `provider_agent_ids` entry to `codex`, and set
`timeout_seconds` to 120–600. The actor requires the standard Controller and
Codex worker to be active, uses only fixed service names and prompts, restores
the initially active units on failure, and writes no live evidence to Git.
Successful repository tests prove this control flow only; `e2e-run` against a
specific deployed revision is the live evidence.

The `model_menu` check follows the complete callback ladder in the dedicated
topic: it selects the first provider, first model, and first effort exposed by
the Hub, then requires the deterministic final confirmation. This changes only
the canary topic's active session and never sends a productive model prompt.
The `reply_route` check first obtains a response through an explicit provider
mention, then sends a real Telegram Reply without another mention and requires
the response to come from the original provider identity.
The `forwarded_quote` check forwards a harmless provider marker back into the
topic, verifies that the forward alone receives no provider answer, then checks
that it is visible to the next explicit turn as quoted context.

The `burst_route` check sends one harmless instruction as three concurrent
Telegram API requests and requires one coherent provider answer. The
`stop_route` check must appear after `model_menu`: it mentions the first
provider selected there, starts a harmless wait, sends deterministic `stop`,
requires the Hub acknowledgement, and proves that a new turn works afterward.
The stop applies to the whole topic, so it interrupts the mentioned provider
even when an earlier check left another agent active.
