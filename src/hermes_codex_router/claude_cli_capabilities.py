"""Bounded, passive inspection of a Claude CLI's advertised text controls.

Help text is an advertisement only. It does not attest to route, approval host,
tool isolation, subscription mode, or any behavior of a productive turn.
"""

from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .owned_process_exit import peek_exit_code

_PROBE_TIMEOUT_SECONDS = 5.0
_MAX_STDOUT_BYTES = 128 * 1024
_MAX_STDERR_BYTES = 64 * 1024
_ERROR_MESSAGE = "Claude CLI capabilities could not be verified."
_REQUIRED_OPTIONS = frozenset(
    {
        "print",
        "output-format",
        "verbose",
        "restricted",
        "safe-mode",
        "strict-mcp-config",
        "disable-slash-commands",
        "settings",
        "permission-mode",
        "permission-prompts",
        "tools",
        "session-id",
        "resume",
        "model",
        "effort",
    }
)
# Help option rows are indented and have a bare switch, a declared argument,
# or a description separated by at least two spaces. Prose mentions and
# single-space sentences do not become declarations.
_OPTION_ENTRY = re.compile(
    r"^ {2}(?:-[A-Za-z],\s*)?--(?P<name>[a-z][a-z0-9-]*)"
    r"(?:[ =](?:<[^>\n]+>|\[[^\]\n]+\]|[A-Z][A-Z0-9_-]*))?"
    r"(?: {2,}.*)?$"
)
_NONE = re.compile(r"(?<![\w-])none(?![\w-])")
_DONT_ASK = re.compile(r"(?<![\w-])dontAsk(?![\w-])")


class ClaudeCliCapabilityError(RuntimeError):
    """A safe, fixed public failure without CLI output or OS diagnostics."""

    def __init__(self) -> None:
        super().__init__(_ERROR_MESSAGE)


class ClaudeCliUnavailableError(ClaudeCliCapabilityError):
    """The configured executable cannot be resolved before inspection."""


def _fingerprint(path: str) -> tuple[int, int, int, int, int]:
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ClaudeCliCapabilityError()
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _advertised_options(help_text: str) -> dict[str, list[str]]:
    entries: dict[str, list[str]] = {}
    current_name: str | None = None
    current_lines: list[str] = []

    def finish() -> None:
        if current_name is not None:
            entries.setdefault(current_name, []).append("\n".join(current_lines))

    for line in help_text.splitlines():
        match = _OPTION_ENTRY.fullmatch(line)
        if match is not None:
            finish()
            current_name = match.group("name")
            current_lines = [line]
        elif current_name is not None and (not line.strip() or line.startswith("    ")):
            current_lines.append(line)
        else:
            finish()
            current_name = None
            current_lines = []
    finish()
    return entries


def _has_required_advertisement(
    output: bytes, *, file_tools: bool = False, image_input: bool = False
) -> bool:
    entries = _advertised_options(output.decode("utf-8", errors="replace"))
    return (
        _REQUIRED_OPTIONS.issubset(entries)
        and any(_NONE.search(stanza) for stanza in entries["permission-prompts"])
        and any(_DONT_ASK.search(stanza) for stanza in entries["permission-mode"])
        and (
            not image_input
            or "input-format" in entries
            and "replay-user-messages" in entries
            and any(
                re.search(r"(?<![\w-])stream-json(?![\w-])", stanza)
                for stanza in entries["input-format"]
            )
        )
        and (
            not file_tools
            or "setting-sources" in entries
            and any(
                re.search(r"(?<![\w-])manual(?![\w-])", stanza)
                for stanza in entries["permission-mode"]
            )
        )
    )


def _kill_and_reap(process: subprocess.Popen[bytes]) -> None:
    # The leader may have exited while a descendant still holds a pipe. Kill
    # the owned session group before reaping the leader to avoid a PID reuse.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)
    finally:
        for pipe in (process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()


def _read_help(
    path: str, *, cwd: Path, environment: dict[str, str], interrupted: threading.Event
) -> bytes:
    if interrupted.is_set():
        raise ClaudeCliCapabilityError()
    deadline = time.monotonic() + _PROBE_TIMEOUT_SECONDS
    process = subprocess.Popen(
        [path, "--help"],
        cwd=cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    output = bytearray()
    diagnostic_bytes = 0
    try:
        assert process.stdout is not None and process.stderr is not None
        with selectors.DefaultSelector() as selector:
            for pipe in (process.stdout, process.stderr):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map():
                if interrupted.is_set() or time.monotonic() >= deadline:
                    raise ClaudeCliCapabilityError()
                for key, _ in selector.select(min(0.05, deadline - time.monotonic())):
                    try:
                        chunk = os.read(key.fd, 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                    elif key.fileobj is process.stdout:
                        output.extend(chunk)
                        if len(output) > _MAX_STDOUT_BYTES:
                            raise ClaudeCliCapabilityError()
                    else:
                        diagnostic_bytes += len(chunk)
                        if diagnostic_bytes > _MAX_STDERR_BYTES:
                            raise ClaudeCliCapabilityError()
            while (exit_code := peek_exit_code(process)) is None:
                if interrupted.is_set() or time.monotonic() >= deadline:
                    raise ClaudeCliCapabilityError()
                interrupted.wait(min(0.05, deadline - time.monotonic()))
        if interrupted.is_set() or time.monotonic() >= deadline or exit_code != 0:
            raise ClaudeCliCapabilityError()
        return bytes(output)
    finally:
        _kill_and_reap(process)


class ClaudeCliCapabilities:
    """Cache only successful help inspections for an unchanged executable."""

    def __init__(self) -> None:
        self._success: dict[tuple[str, bool, bool], tuple[int, int, int, int, int]] = {}
        self._lock = threading.Lock()

    def require(
        self,
        executable: str,
        *,
        cwd: Path,
        environment: dict[str, str],
        interrupted: threading.Event,
        file_tools: bool = False,
        image_input: bool = False,
    ) -> str:
        """Return the resolved CLI path if its current help advertises all controls."""
        if interrupted.is_set():
            raise ClaudeCliCapabilityError()
        try:
            search_path = os.pathsep.join(
                entry
                for entry in environment.get("PATH", os.defpath).split(os.pathsep)
                if os.path.isabs(entry)
            )
            selected = executable
            if os.path.dirname(selected) and not os.path.isabs(selected):
                raise ClaudeCliCapabilityError()
            if not os.path.isabs(selected) and not search_path:
                raise ClaudeCliUnavailableError()
            path = shutil.which(selected, path=search_path)
            if path is None:
                raise ClaudeCliUnavailableError()
            path = os.path.realpath(path)
            before = _fingerprint(path)
            with self._lock:
                if self._success.get((path, file_tools, image_input)) == before:
                    if interrupted.is_set():
                        raise ClaudeCliCapabilityError()
                    return path
            with tempfile.TemporaryDirectory(prefix="hub-claude-capability-") as directory:
                isolated = {
                    key: value
                    for key, value in environment.items()
                    if key in {"PATH", "LANG", "LC_ALL", "TZ"}
                }
                isolated.update(
                    HOME=directory,
                    CLAUDE_CONFIG_DIR=str(Path(directory) / ".claude"),
                    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
                    DISABLE_AUTOUPDATER="1",
                )
                output = _read_help(
                    path, cwd=Path(directory), environment=isolated, interrupted=interrupted
                )
            if not _has_required_advertisement(
                output, file_tools=file_tools, image_input=image_input
            ):
                raise ClaudeCliCapabilityError()
            after = _fingerprint(path)
            if interrupted.is_set() or after != before:
                raise ClaudeCliCapabilityError()
            with self._lock:
                self._success[(path, file_tools, image_input)] = after
            return path
        except ClaudeCliCapabilityError:
            raise
        except Exception:
            raise ClaudeCliCapabilityError() from None
