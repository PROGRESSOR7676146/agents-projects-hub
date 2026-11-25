"""Model-free behavioral gate for one Claude CLI build under this Hub release (ADR 0113).

The host side starts a ``bwrap`` namespace with its own network namespace and an
empty home. Inside it, the actor serves a fictional local Messages API and drives
the production host-profile adapter: production argv, environment and parser.
Nothing can reach a provider: the namespace has only its own loopback.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence, cast

GATE_VERSION = 1
BWRAP = Path("/usr/bin/bwrap")
CANDIDATE = "/tmp/hub-claude-gate/candidate/claude"
PREVIOUS = "/tmp/hub-claude-gate/previous/claude"
MODEL = "claude-example-gate"
MAX_OUTPUT = 64 * 1024
MAX_REQUESTS = 32
_VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,6}")
OVERLOADED_CODE = "claude_provider_overloaded"


class ClaudeCliGateError(RuntimeError):
    """The gate could not run; never a verdict about the candidate."""


def valid_version(value: object) -> bool:
    return isinstance(value, str) and _VERSION.fullmatch(value) is not None


# ---------------------------------------------------------------------------
# Host side


def _bwrap() -> Path:
    try:
        info = BWRAP.stat()
    except OSError:
        raise ClaudeCliGateError("gate_namespace_unavailable") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ClaudeCliGateError("gate_namespace_unavailable")
    return BWRAP


def _regular(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise ClaudeCliGateError("gate_executable_invalid")
    return resolved


def _symlink_hops(path: Path, within: Path) -> list[tuple[str, str]]:
    """Return (target, link) for every symlink under ``within`` on the way to ``path``."""
    hops: list[tuple[str, str]] = []
    pending = Path(os.path.normpath(os.path.abspath(path)))
    for _ in range(32):
        current = Path("/")
        for part in pending.parts[1:]:
            current = current / part
            if current.is_symlink():
                target = os.readlink(current)
                if current.is_relative_to(within):
                    hops.append((target, str(current)))
                rest = pending.relative_to(current)
                pending = Path(os.path.normpath(current.parent / target / rest))
                break
        else:
            return hops
    raise ClaudeCliGateError("gate_interpreter_invalid")


# Only system code and the few configuration files a process start needs; no /home,
# /mnt, /run, /var, /srv, /opt or the rest of /etc.
_SYSTEM_ROOTS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32")
_ETC_FILES = (
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    "/etc/ld.so.cache",
    "/etc/localtime",
    "/etc/hosts",
)


def gate_argv(candidate: Path, previous: Path | None, version: str) -> list[str]:
    """Return the namespace argv: an allowlisted read-only filesystem and no network."""
    home = Path.home()
    python = Path(os.path.normpath(os.path.abspath(sys.executable)))
    package = Path(__file__).resolve().parent
    argv = [
        str(_bwrap()),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--tmpfs",
        str(home),
    ]
    shown: list[Path] = []
    for root in _SYSTEM_ROOTS:
        path = Path(root)
        if path.is_symlink():
            argv += ["--symlink", os.readlink(path), root]
        elif path.is_dir():
            argv += ["--ro-bind", root, root]
            shown.append(path)
    for name in _ETC_FILES:
        if Path(name).is_file():
            argv += ["--ro-bind", str(Path(name).resolve()), name]
    # Show exactly the interpreter and the package this release imports.
    hops = _symlink_hops(python, Path("/"))
    sources = (Path(sys.prefix), Path(sys.base_prefix), package.parent, python.resolve().parent)
    extra: list[Path] = []
    for path in sources:
        normal = Path(os.path.normpath(os.path.abspath(path)))
        for item in (normal, normal.resolve()):
            if not any(item.is_relative_to(p) for p in (*shown, *extra)):
                extra.append(item)
    for path in extra:
        argv += ["--ro-bind", str(path), str(path)]
    for target, link in hops:
        if not any(Path(link).is_relative_to(p) for p in (*shown, *extra)):
            argv += ["--symlink", target, link]
    argv += ["--ro-bind", str(_regular(candidate)), CANDIDATE]
    if previous is not None:
        argv += ["--ro-bind", str(_regular(previous)), PREVIOUS]
    argv += [
        "--clearenv",
        "--setenv",
        "PATH",
        "/usr/bin:/bin",
        "--setenv",
        "HOME",
        str(home),
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "HUB_GATE_SOURCES",
        ":".join(
            [str(p) for p in extra if p.is_relative_to(home)]
            + [link for _, link in hops if Path(link).is_relative_to(home)]
        ),
        "--",
        str(python),
        "-I",
        "-c",
        # Import exactly this release's package, never another one on sys.path.
        "import sys; sys.path.insert(0, sys.argv[1]); del sys.argv[1]; "
        "from hermes_codex_router.claude_cli_gate import main; raise SystemExit(main())",
        str(package.parent),
        "--actor",
        "--version",
        version,
    ]
    if previous is not None:
        argv.append("--with-previous")
    return argv


def run_gate(
    candidate: Path, version: str, *, previous: Path | None = None, timeout: float = 240
) -> dict[str, Any]:
    """Run the gate; return its report. ``passed`` is the only verdict."""
    if not valid_version(version):
        raise ClaudeCliGateError("gate_version_invalid")
    argv = gate_argv(candidate, previous, version)
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"gate_version": GATE_VERSION, "passed": False, "failure": "gate_timeout"}
    except OSError:
        raise ClaudeCliGateError("gate_namespace_unavailable") from None
    try:
        report = json.loads(completed.stdout[:MAX_OUTPUT])
    except ValueError:
        report = None
    if not isinstance(report, dict) or report.get("gate_version") != GATE_VERSION:
        # The namespace or the actor did not run: no verdict about the candidate.
        raise ClaudeCliGateError("gate_report_invalid")
    if report.get("unavailable") is True:
        raise ClaudeCliGateError(str(report.get("failure"))[:64])
    report["passed"] = report.get("passed") is True and completed.returncode == 0
    return report


def post_check(executable: Path, version: str) -> dict[str, Any]:
    """After a switch: the stable path's version and advertised controls, as a worker runs it."""
    from .claude_cli_capabilities import ClaudeCliCapabilities

    report: dict[str, Any] = {"gate_version": GATE_VERSION, "passed": False, "post_check": True}
    with tempfile.TemporaryDirectory(prefix="hub-claude-post-check-") as directory:
        environment = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": directory,
            "CLAUDE_CONFIG_DIR": str(Path(directory) / ".claude"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
        }
        try:
            completed = subprocess.run(
                [str(executable), "--version"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            report["failure"] = "version_unavailable"
            return report
        if completed.returncode != 0 or completed.stdout.strip() != version + " (Claude Code)":
            report["failure"] = "version_mismatch"
            return report
        capabilities = ClaudeCliCapabilities()
        for host in (True, False):
            try:
                capabilities.require(
                    str(executable),
                    cwd=Path(directory),
                    environment=environment,
                    interrupted=threading.Event(),
                    **({"host": True} if host else {}),
                )
            except Exception:
                report["failure"] = "capability_host" if host else "capability_text_only"
                return report
    report["passed"] = True
    return report


# ---------------------------------------------------------------------------
# Namespace side


_NONCE = re.compile(r"hub-gate-nonce-[0-9a-f]{16}")


def _texts(content: object) -> list[str]:
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [
            block["text"]
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
    return []


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.lock = threading.Lock()
        self.overloaded = False
        self.requests = 0
        self.rejections = 0
        self.models: list[str] = []
        # Per answered request: nonces in user turns and the assistant replies.
        self.history: list[tuple[frozenset[str], frozenset[str]]] = []


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def gate(self) -> _Server:
        return cast(_Server, self.server)

    def log_message(self, format: str, *args: object) -> None:
        pass

    def _respond(self, code: int, content_type: str, data: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if data:
            self.wfile.write(data)

    def _count(self) -> bool:
        with self.gate.lock:
            self.gate.requests += 1
            return self.gate.requests <= MAX_REQUESTS

    def do_HEAD(self) -> None:
        self._respond(200 if self._count() else 429, "application/json", b"")

    def do_GET(self) -> None:
        self._count()
        self._respond(404, "application/json", b"{}")

    def do_POST(self) -> None:
        if not self._count():
            self._respond(429, "application/json", b"{}")
            return
        size = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(min(size, 8 * 1024 * 1024)))
        except ValueError:
            body = None
        path = self.path.split("?", 1)[0]
        messages = body.get("messages") if isinstance(body, dict) else None
        if path != "/v1/messages" or not isinstance(body, dict) or not isinstance(messages, list):
            self._respond(404, "application/json", b"{}")
            return
        asked: set[str] = set()
        answered: set[str] = set()
        last: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            texts = _texts(message.get("content"))
            found = [n for text in texts for n in _NONCE.findall(text)]
            if message.get("role") == "user":
                asked.update(found)
                last = found or last
            elif message.get("role") == "assistant":
                answered.update(text.strip()[:64] for text in texts)
        with self.gate.lock:
            self.gate.models.append(str(body.get("model")))
            overloaded = self.gate.overloaded
            if overloaded:
                self.gate.rejections += 1
            else:
                self.gate.history.append((frozenset(asked), frozenset(answered)))
        if overloaded:
            error = {"type": "error", "error": {"type": "overloaded_error", "message": "example"}}
            self._respond(529, "application/json", json.dumps(error).encode())
            return
        reply = "ack " + (last[-1] if last else "none")
        usage = {
            "input_tokens": 3,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 0},
            "output_tokens": 2,
        }
        message = {
            "id": "msg_example_gate",
            "type": "message",
            "role": "assistant",
            "model": str(body.get("model")),
            "content": [{"type": "text", "text": reply}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": usage,
        }
        if body.get("stream") is not True:
            self._respond(200, "application/json", json.dumps(message).encode())
            return
        start = dict(message, content=[], stop_reason=None, usage={**usage, "output_tokens": 1})
        events = [
            {"type": "message_start", "message": start},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": reply},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
            {"type": "message_stop"},
        ]
        data = "".join(
            "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events
        )
        self._respond(200, "text/event-stream", data.encode())


def _isolated() -> bool:
    """The actor runs only where it cannot reach a provider or the owner's files."""
    try:
        interfaces = [
            line.split(":", 1)[0].strip()
            for line in Path("/proc/net/dev").read_text().splitlines()[2:]
        ]
        home = Path(os.environ.get("HOME", "/nonexistent"))
        shown = [Path(p) for p in os.environ.get("HUB_GATE_SOURCES", "").split(":") if p]
        return (
            interfaces == ["lo"]
            and home.is_dir()
            and _only_mount_points(home, shown)
            and not any(Path(p).exists() for p in ("/run", "/mnt", "/var", "/srv", "/opt"))
            and {p.name for p in Path("/etc").iterdir()} <= {Path(n).name for n in _ETC_FILES}
        )
    except OSError:
        return False


def _only_mount_points(directory: Path, shown: list[Path]) -> bool:
    """Below HOME only the parents of the bound release sources may exist."""
    for entry in directory.iterdir():
        if any(entry == source for source in shown):
            continue
        if not entry.is_dir() or entry.is_symlink():
            return False
        if not any(source.is_relative_to(entry) for source in shown):
            return False
        if not _only_mount_points(entry, shown):
            return False
    return True


def _actor(version: str, with_previous: bool) -> dict[str, Any]:
    from .claude_cli_capabilities import ClaudeCliCapabilities
    from .claude_host_profile import ClaudeHostConfig
    from .claude_stream import ClaudeTerminalFailure
    from .external_runtime import ExternalCliAdapter, ProviderUnavailableError

    checks: list[str] = []
    report: dict[str, Any] = {"gate_version": GATE_VERSION, "passed": False, "checks": checks}

    def fail(code: str) -> dict[str, Any]:
        report["failure"] = code
        return report

    if not _isolated():
        report["unavailable"] = True
        return fail("gate_not_isolated")
    server = _Server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    work = Path(tempfile.mkdtemp(prefix="hub-claude-gate-"))
    project = work / "project"
    provider_home = work / "provider-home"
    project.mkdir()
    provider_home.mkdir(mode=0o700)
    os.environ.update(
        ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % server.server_port,
        ANTHROPIC_AUTH_TOKEN="example-gate-token",
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_AUTOUPDATER="1",
    )

    try:
        completed = subprocess.run(
            [CANDIDATE, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=project,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return fail("version_unavailable")
    if completed.returncode != 0 or completed.stdout.strip() != version + " (Claude Code)":
        return fail("version_mismatch")
    checks.append("version")

    capabilities = ClaudeCliCapabilities()
    environment = dict(os.environ)
    for host in (True, False):
        try:
            capabilities.require(
                CANDIDATE,
                cwd=project,
                environment=environment,
                interrupted=threading.Event(),
                **({"host": True} if host else {}),
            )
        except Exception:
            return fail("capability_host" if host else "capability_text_only")
    checks.append("capabilities")

    def turn(executable: str, nonce: str, **kwargs: Any) -> Any:
        adapter = ExternalCliAdapter("claude", executable=executable, claude_auth_route="cpa")
        return adapter.run_turn(
            cwd=project,
            prompt="Hub gate check " + nonce + ".",
            model=MODEL,
            effort="high",
            timeout=90,
            claude_sandbox=ClaudeHostConfig(
                project_root=project,
                provider_home=provider_home,
                claude_executable=Path(executable),
            ),
            **kwargs,
        )

    def successful(result: Any, session: str, nonce: str) -> bool:
        usage = result.usage if isinstance(result.usage, dict) else {}
        # Bounded booleans only; never native text.
        observed = {
            "text": result.text.strip() == "ack " + nonce,
            "session": result.provider_session_id == session,
            # The status footer needs per-request context (REQ-CMD-001, ADR 0098).
            "usage": isinstance(usage.get("requests"), int)
            and usage["requests"] >= 1
            and isinstance(usage.get("last_context"), int),
        }
        report["observed"] = observed
        return all(observed.values())

    def nonce() -> str:
        return "hub-gate-nonce-" + uuid.uuid4().hex[:16]

    session = str(uuid.uuid4())
    writer = PREVIOUS if with_previous else CANDIDATE
    first_nonce = nonce()
    try:
        first = turn(writer, first_nonce, new_session_id=session)
    except Exception:
        return fail("previous_session_failed" if with_previous else "new_session_failed")
    if not successful(first, session, first_nonce):
        return fail("previous_session_invalid" if with_previous else "new_session_invalid")
    if not with_previous:
        checks.append("new_session")
    with server.lock:
        before = len(server.history)
    second_nonce = nonce()
    try:
        resumed = turn(CANDIDATE, second_nonce, session_id=session)
    except Exception:
        return fail("resume_failed")
    if not successful(resumed, session, second_nonce):
        return fail("resume_invalid")
    with server.lock:
        sent = server.history[before:]
    # The resumed request must carry the earlier exchange, not just reuse its identity.
    if not any(
        {first_nonce, second_nonce} <= asked and "ack " + first_nonce in answered
        for asked, answered in sent
    ):
        return fail("resume_history_missing")
    checks.append("resume_previous" if with_previous else "resume")
    if with_previous:
        fresh_session, fresh_nonce = str(uuid.uuid4()), nonce()
        try:
            fresh = turn(CANDIDATE, fresh_nonce, new_session_id=fresh_session)
        except Exception:
            return fail("new_session_failed")
        if not successful(fresh, fresh_session, fresh_nonce):
            return fail("new_session_invalid")
        checks.append("new_session")
    with server.lock:
        server.overloaded = True
        models = set(server.models)
    if models != {MODEL}:
        return fail("model_selection_invalid")
    checks.append("model_selection")
    # Production retries an overload with backoff; the gate checks only the terminal shape.
    os.environ["CLAUDE_CODE_MAX_RETRIES"] = "0"
    try:
        turn(CANDIDATE, nonce(), new_session_id=str(uuid.uuid4()))
    except (ClaudeTerminalFailure, ProviderUnavailableError) as error:
        # Hub shows a distinct overload notice; any other classification is drift.
        if getattr(error, "code", None) != OVERLOADED_CODE:
            return fail("overloaded_misclassified")
    except Exception as error:
        report["overloaded_error"] = type(error).__name__[:64]
        return fail("overloaded_unclassified")
    else:
        return fail("overloaded_succeeded")
    with server.lock:
        rejected = server.rejections
    if rejected < 1:
        return fail("overloaded_not_requested")
    checks.append("overloaded")
    report["passed"] = True
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Model-free Claude CLI gate (ADR 0113)")
    parser.add_argument("--actor", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--with-previous", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--post-check", action="store_true")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    if args.actor:
        if not valid_version(args.version):
            return 3
        report = _actor(args.version, args.with_previous)
    else:
        if args.candidate is None:
            parser.error("--candidate is required")
        if not valid_version(args.version):
            print(
                json.dumps(
                    {
                        "gate_version": GATE_VERSION,
                        "passed": False,
                        "failure": "gate_version_invalid",
                    }
                )
            )
            return 3
        if args.post_check:
            report = post_check(args.candidate, args.version)
            print(json.dumps(report, sort_keys=True))
            return 0 if report.get("passed") is True else 1
        try:
            report = run_gate(args.candidate, args.version, previous=args.previous)
        except (ClaudeCliGateError, OSError) as error:
            print(
                json.dumps({"gate_version": GATE_VERSION, "passed": False, "failure": str(error)})
            )
            return 3
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
