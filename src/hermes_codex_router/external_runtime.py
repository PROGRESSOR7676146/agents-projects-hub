from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from .antigravity_model import model_arguments
from .claude_stream import (
    MAX_CLAUDE_STDERR_BYTES,
    ClaudeStreamError,
    ClaudeStreamReader,
    VisibleAssistantCallback,
    parse_claude_stream,
)
from .provider_limits import ProviderLimit, parse_antigravity_limit, parse_opencode_limit

Run = Callable[..., subprocess.CompletedProcess[str]]


class ExternalRuntimeError(RuntimeError):
    pass


class ProviderLimitError(ExternalRuntimeError):
    def __init__(self, limit: ProviderLimit) -> None:
        super().__init__(
            f"{limit.provider} {limit.window} limit exhausted; reset telemetry recorded"
        )
        self.limit = limit


class ProviderUnavailableError(ExternalRuntimeError):
    def __init__(self, code: str, public_message: str) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


class ExternalTurnInterrupted(ExternalRuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ExternalTurnResult:
    runtime: str
    text: str
    provider_session_id: str | None
    model: str | None


def _json_values(output: str) -> list[dict[str, object]]:
    values: list[dict[str, object]] = []
    stripped = output.strip()
    if not stripped:
        return values
    try:
        value = json.loads(stripped)
    except json.JSONDecodeError:
        for line in stripped.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                values.append(value)
    else:
        if isinstance(value, dict):
            values.append(value)
    return values


def _claude_cpa_environment(environment: dict[str, str]) -> None:
    """Refuse an ambiguous route before a productive Claude CLI invocation."""
    try:
        route = urlsplit(environment.get("ANTHROPIC_BASE_URL", ""))
        local_port = route.port
    except ValueError as exc:
        raise ProviderUnavailableError(
            "claude_cpa_route_unverified", "Claude requires an explicit local CPA route."
        ) from exc
    if (
        route.scheme != "http"
        or route.hostname not in {"127.0.0.1", "::1"}
        or local_port is None
        or route.username is not None
        or route.password is not None
        or route.path not in {"", "/"}
        or route.query
        or route.fragment
    ):
        raise ProviderUnavailableError(
            "claude_cpa_route_unverified", "Claude requires an explicit local CPA route."
        )
    if bool(environment.get("ANTHROPIC_AUTH_TOKEN")) == bool(
        environment.get("ANTHROPIC_API_KEY")
    ) or any(
        environment.get(key)
        for key in (
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        )
    ):
        raise ProviderUnavailableError(
            "claude_cpa_credential_ambiguous",
            "Claude CPA credentials or provider selection are ambiguous.",
        )


class ExternalCliAdapter:
    def __init__(
        self,
        runtime: str,
        *,
        executable: str | None = None,
        runtime_home: Path | None = None,
        opencode_log_path: Path | None = None,
        antigravity_log_path: Path | None = None,
        run: Run = subprocess.run,
    ) -> None:
        if runtime not in {"gemini", "antigravity", "opencode", "claude"}:
            raise ExternalRuntimeError(f"unsupported external runtime: {runtime}")
        self.runtime = runtime
        self.executable = executable or ("agy" if runtime == "antigravity" else runtime)
        self.runtime_home = runtime_home.expanduser().resolve(strict=True) if runtime_home else None
        self.opencode_log_path = (
            opencode_log_path.expanduser().resolve(strict=False)
            if opencode_log_path is not None
            else Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
            / "opencode/log/opencode.log"
        )
        self.antigravity_log_path = (
            antigravity_log_path.expanduser().resolve(strict=False)
            if antigravity_log_path is not None
            else None
        )
        self._run = run
        self._uses_default_runner = run is subprocess.run
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen[str] | None = None
        self._interrupt_requested = threading.Event()

    def interrupt(self) -> bool:
        """Terminate only this adapter's active provider process group."""
        self._interrupt_requested.set()
        with self._process_lock:
            process = self._active_process
        if process is None or process.poll() is not None:
            return False
        try:
            # This path is reserved for the user's emergency stop.  A provider
            # may ignore SIGTERM while it is inside its own model/runtime loop,
            # so terminate the isolated process group deterministically.
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return False
        return True

    def prepare_interruptible_turn(self) -> None:
        """Clear a prior interrupt before a worker starts its monitor."""
        with self._process_lock:
            if self._active_process is not None and self._active_process.poll() is None:
                raise ExternalRuntimeError("provider process is already active")
            self._interrupt_requested.clear()

    def build_argv(
        self,
        *,
        cwd: Path,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        new_session_id: str | None = None,
    ) -> tuple[str, ...]:
        canonical_cwd = cwd.expanduser().resolve(strict=True)
        if not prompt.strip():
            raise ExternalRuntimeError("prompt is empty")
        if new_session_id is not None and self.runtime != "claude":
            raise ProviderUnavailableError(
                "external_session_identity_unsupported",
                "Caller-chosen session identity is unsupported for this runtime.",
            )
        if self.runtime == "gemini":
            argv = [
                self.executable,
                "--output-format",
                "json",
                "--sandbox",
                "--approval-mode",
                "default",
            ]
            if session_id:
                argv.extend(("--resume", session_id))
            if model:
                argv.extend(("--model", model))
            argv.extend(("--prompt", prompt))
            return tuple(argv)
        if self.runtime == "antigravity":
            argv = [
                self.executable,
                "--print",
                prompt,
                "--output-format",
                "json",
                "--sandbox",
                "--mode",
                "accept-edits",
                "--print-timeout",
                "15m",
            ]
            if session_id:
                argv.extend(("--conversation", session_id))
            argv.extend(model_arguments(model, effort))
            return tuple(argv)
        if self.runtime == "claude":
            if new_session_id is not None and session_id is not None:
                raise ProviderUnavailableError(
                    "claude_identity_ambiguous", "Claude cannot start and resume simultaneously."
                )
            if session_id is not None or new_session_id is not None:
                identity = session_id if session_id is not None else new_session_id
                if not isinstance(identity, str):
                    raise ProviderUnavailableError(
                        "claude_resume_invalid", "Claude session identity is invalid."
                    )
                try:
                    uuid.UUID(identity)
                except ValueError as exc:
                    raise ProviderUnavailableError(
                        "claude_resume_invalid", "Claude session identity is invalid."
                    ) from exc
            argv = [
                self.executable,
                "--print",
                "--output-format",
                "stream-json",
                "--verbose",
                "--restricted",
                "--safe-mode",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--permission-mode",
                "dontAsk",
                "--permission-prompts",
                "none",
                "--tools",
                "",
            ]
            if session_id:
                argv.extend(("--resume", session_id))
            if new_session_id is not None:
                argv.extend(("--session-id", new_session_id))
            if model and model != "unknown":
                argv.extend(("--model", model))
            if effort and effort not in {"none", "minimal"}:
                if effort not in {"low", "medium", "high", "xhigh", "max"}:
                    raise ProviderUnavailableError(
                        "claude_effort_unsupported", "Claude effort is unsupported."
                    )
                argv.extend(("--effort", effort))
            # `--tools` is variadic, so the prompt must not directly follow it.
            argv.extend(("--", prompt))
            return tuple(argv)
        argv = [
            self.executable,
            "run",
            "--format",
            "json",
            "--dir",
            str(canonical_cwd),
        ]
        if session_id:
            argv.extend(("--session", session_id))
        if model:
            argv.extend(("--model", model))
        if effort and effort != "default":
            argv.extend(("--variant", effort))
        argv.append(prompt)
        return tuple(argv)

    def run_turn(
        self,
        *,
        cwd: Path,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        timeout: float = 900,
        interrupt_prepared: bool = False,
        staging_dir: Path | None = None,
        new_session_id: str | None = None,
        on_visible_assistant: VisibleAssistantCallback | None = None,
    ) -> ExternalTurnResult:
        argv = self.build_argv(
            cwd=cwd,
            prompt=prompt,
            session_id=session_id,
            model=model,
            effort=effort,
            new_session_id=new_session_id,
        )
        environment = os.environ.copy()
        if self.runtime == "claude":
            _claude_cpa_environment(environment)
        if staging_dir is not None:
            environment["HUB_STAGING_DIR"] = str(staging_dir)
            environment["HUB_ARTIFACTS_DIR"] = str(staging_dir)
        if self.runtime == "gemini" and self.runtime_home is not None:
            environment["GEMINI_CLI_HOME"] = str(self.runtime_home)
        if not interrupt_prepared:
            self.prepare_interruptible_turn()
        detected_limit: list[ProviderLimit] = []
        antigravity_log = ""
        owned_antigravity_log = False
        active_antigravity_log_path: Path | None = None
        if self._uses_default_runner and self.runtime == "claude":
            result = self._run_claude_process(
                argv,
                cwd=cwd,
                environment=environment,
                timeout=timeout,
                expected_session_id=session_id if session_id is not None else new_session_id,
                on_visible_assistant=on_visible_assistant,
            )
        elif self._uses_default_runner:
            if self.runtime == "antigravity":
                if self.antigravity_log_path is None:
                    descriptor, temporary_log = tempfile.mkstemp(prefix="hub-agy-", suffix=".log")
                    os.close(descriptor)
                    active_antigravity_log_path = Path(temporary_log)
                    owned_antigravity_log = True
                else:
                    active_antigravity_log_path = self.antigravity_log_path
                    active_antigravity_log_path.write_text("", encoding="utf-8")
                    active_antigravity_log_path.chmod(0o600)
                argv = (*argv, "--log-file", str(active_antigravity_log_path))
            log_offset: int | None = None
            if self.runtime == "opencode":
                try:
                    if self.opencode_log_path.is_file() and not self.opencode_log_path.is_symlink():
                        log_offset = self.opencode_log_path.stat().st_size
                except OSError:
                    pass
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
            except OSError as exc:
                if self.runtime == "claude":
                    raise ProviderUnavailableError(
                        "claude_cli_unavailable", "Claude CLI could not be started."
                    ) from exc
                raise
            with self._process_lock:
                self._active_process = process
            limit_stop = threading.Event()

            def watch_opencode_limit() -> None:
                if log_offset is None:
                    return
                offset = log_offset
                carry = ""
                while not limit_stop.wait(0.2):
                    try:
                        size = self.opencode_log_path.stat().st_size
                        if size < offset:
                            offset = 0
                        if size == offset:
                            continue
                        with self.opencode_log_path.open("rb") as log:
                            log.seek(offset)
                            appended = log.read(min(size - offset, 131072))
                            offset = log.tell()
                    except OSError:
                        continue
                    sample = (carry + appended.decode("utf-8", errors="replace"))[-135168:]
                    limit = parse_opencode_limit(sample)
                    carry = sample[-4096:]
                    if limit is None:
                        continue
                    detected_limit.append(limit)
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    return

            limit_monitor = threading.Thread(
                target=watch_opencode_limit,
                name="opencode-limit-monitor",
                daemon=True,
            )
            limit_monitor.start()
            try:
                try:
                    stdout, stderr = process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        stdout, stderr = process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        stdout, stderr = process.communicate(timeout=5)
                    raise ExternalRuntimeError(f"{self.runtime} timed out safely")
                result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
            finally:
                limit_stop.set()
                limit_monitor.join(timeout=1)
                if self.runtime == "antigravity" and active_antigravity_log_path is not None:
                    try:
                        with active_antigravity_log_path.open("rb") as log:
                            size = active_antigravity_log_path.stat().st_size
                            log.seek(max(0, size - 262144))
                            antigravity_log = log.read(262144).decode("utf-8", errors="replace")
                    except OSError:
                        pass
                    if owned_antigravity_log:
                        try:
                            active_antigravity_log_path.unlink()
                        except OSError:
                            pass
                with self._process_lock:
                    if self._active_process is process:
                        self._active_process = None
        else:
            result = self._run(
                argv,
                cwd=cwd,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if self.runtime == "claude":
                if len(result.stderr.encode("utf-8")) > MAX_CLAUDE_STDERR_BYTES:
                    raise ClaudeStreamError("claude diagnostic output exceeded its limit")
                reader = ClaudeStreamReader(
                    expected_session_id=session_id if session_id is not None else new_session_id,
                    on_visible_assistant=on_visible_assistant,
                )
                reader.feed(result.stdout.encode("utf-8"))
                reader.finish()
        if self._interrupt_requested.is_set():
            raise ExternalTurnInterrupted(f"{self.runtime} turn interrupted by user")
        if detected_limit:
            raise ProviderLimitError(detected_limit[0])
        return self._parse_result(
            result,
            session_id=session_id,
            new_session_id=new_session_id,
            model=model,
            antigravity_log=antigravity_log,
        )

    def _run_claude_process(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout: float,
        expected_session_id: str | None,
        on_visible_assistant: VisibleAssistantCallback | None,
    ) -> subprocess.CompletedProcess[str]:
        """Drain both pipes without communicate() or an unbounded reader queue."""
        if self._interrupt_requested.is_set():
            raise ExternalTurnInterrupted("claude turn interrupted by user")
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise ProviderUnavailableError(
                "claude_cli_unavailable", "Claude CLI could not be started."
            ) from exc
        with self._process_lock:
            self._active_process = process
        reader = ClaudeStreamReader(
            expected_session_id=expected_session_id, on_visible_assistant=on_visible_assistant
        )
        deadline = time.monotonic() + timeout
        diagnostic_bytes = 0
        try:
            assert process.stdout is not None and process.stderr is not None
            with selectors.DefaultSelector() as selector:
                for pipe in (process.stdout, process.stderr):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ)
                while selector.get_map():
                    if self._interrupt_requested.is_set():
                        raise ExternalTurnInterrupted("claude turn interrupted by user")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    for key, _ in selector.select(min(0.1, remaining)):
                        try:
                            chunk = os.read(key.fd, 65536)
                        except BlockingIOError:
                            continue
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif key.fileobj is process.stdout:
                            reader.feed(chunk)
                        else:
                            diagnostic_bytes += len(chunk)
                            if diagnostic_bytes > MAX_CLAUDE_STDERR_BYTES:
                                raise ClaudeStreamError(
                                    "claude diagnostic output exceeded its limit"
                                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                process.wait(timeout=remaining)
            stdout = reader.finish()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
            return subprocess.CompletedProcess(argv, process.returncode, stdout, "")
        except subprocess.TimeoutExpired:
            self._terminate_claude_process(process, graceful=True)
            if self._interrupt_requested.is_set():
                raise ExternalTurnInterrupted("claude turn interrupted by user") from None
            raise ExternalRuntimeError("claude timed out safely") from None
        except Exception:
            if self._interrupt_requested.is_set():
                raise ExternalTurnInterrupted("claude turn interrupted by user") from None
            raise
        finally:
            # Kill the owned group even if its leader exited while a descendant
            # retained a pipe. Never leave children after protocol/callback errors.
            try:
                self._terminate_claude_process(process, graceful=False)
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
            finally:
                with self._process_lock:
                    if self._active_process is process:
                        self._active_process = None

    @staticmethod
    def _terminate_claude_process(process: subprocess.Popen[str], *, graceful: bool) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM if graceful else signal.SIGKILL)
        except ProcessLookupError:
            pass
        if graceful:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)

    def _parse_result(
        self,
        result: subprocess.CompletedProcess[str],
        *,
        session_id: str | None,
        new_session_id: str | None,
        model: str | None,
        antigravity_log: str,
    ) -> ExternalTurnResult:
        """Interpret an exited process separately from its lifecycle and stop."""
        if self.runtime == "claude":
            parsed = parse_claude_stream(
                result.stdout,
                expected_session_id=session_id if session_id is not None else new_session_id,
                requested_model=model,
                returncode=result.returncode,
            )
            return ExternalTurnResult("claude", parsed.text, parsed.session_id, parsed.model)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:1000]
            if self.runtime == "opencode" and (limit := parse_opencode_limit(detail)):
                raise ProviderLimitError(limit)
            if self.runtime == "antigravity" and (limit := parse_antigravity_limit(detail)):
                raise ProviderLimitError(limit)
            if self.runtime == "antigravity":
                if limit := parse_antigravity_limit(antigravity_log):
                    raise ProviderLimitError(limit)
                if "User location is not supported for the API use" in antigravity_log:
                    raise ProviderUnavailableError(
                        "unsupported_network_location",
                        "Antigravity is unavailable from the computer's current network location.",
                    )
            raise ExternalRuntimeError(f"{self.runtime} failed safely: {detail}")
        values = _json_values(result.stdout)
        if not values:
            raise ExternalRuntimeError(f"{self.runtime} returned no structured output")
        provider_session_id: str | None = session_id
        detected_model: str | None = model
        text_parts: list[str] = []
        for value in values:
            for key in ("session_id", "sessionId", "sessionID", "conversation_id"):
                if isinstance(value.get(key), str):
                    provider_session_id = str(value[key])
            if isinstance(value.get("model"), str):
                detected_model = str(value["model"])
            for key in ("response", "text", "content"):
                if isinstance(value.get(key), str) and value[key]:
                    text_parts.append(str(value[key]))
            part = value.get("part")
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                text_parts.append(str(part["text"]))
        text = "\n".join(dict.fromkeys(text_parts)).strip()
        if not text:
            raise ExternalRuntimeError(f"{self.runtime} completed without visible text")
        return ExternalTurnResult(self.runtime, text, provider_session_id, detected_model)
