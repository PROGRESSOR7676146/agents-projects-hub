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
from .claude_cli_capabilities import (
    ClaudeCliCapabilities,
    ClaudeCliCapabilityError,
    ClaudeCliUnavailableError,
)
from .claude_file_policy import file_tool_argv, require_file_tool_event, wrap_file_tool_argv
from .claude_file_sandbox import FileToolSandboxConfig, FileToolSandboxError
from .claude_image_input import (
    MAX_CLAUDE_INPUT_BYTES,
    ClaudeImageInputError,
    VerifiedClaudeImage,
    encode_claude_image_input,
)
from .claude_image_receipt import ClaudeImageReceipt
from .claude_native_settings import text_only_settings
from .claude_stream import (
    MAX_CLAUDE_STDERR_BYTES,
    ClaudeStreamError,
    ClaudeStreamReader,
    VisibleAssistantCallback,
    parse_claude_stream,
)
from .diagnostic_log import survived
from .owned_process_exit import peek_exit_code
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
        self._claude_capabilities = ClaudeCliCapabilities()

    def interrupt(self) -> bool:
        """Terminate only this adapter's active provider process group."""
        self._interrupt_requested.set()
        with self._process_lock:
            process = self._active_process
            if process is None:
                return False
            exit_code = peek_exit_code(process) if self.runtime == "claude" else process.poll()
            if exit_code is not None:
                return False
            try:
                # Serialize observation and signal with cleanup/reaping so
                # an emergency stop cannot target a recycled process group.
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return False
            return True

    def prepare_interruptible_turn(self) -> None:
        """Clear a prior interrupt before a worker starts its monitor."""
        with self._process_lock:
            if self._active_process is not None:
                process = self._active_process
                exit_code = peek_exit_code(process) if self.runtime == "claude" else process.poll()
                if exit_code is None:
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
        structured_input: bool = False,
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
                "--settings",
                text_only_settings(),
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
            if structured_input:
                argv.extend(("--input-format", "stream-json", "--replay-user-messages"))
            else:
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
        on_claude_process_started: Callable[[], None] | None = None,
        claude_sandbox: FileToolSandboxConfig | None = None,
        claude_images: tuple[VerifiedClaudeImage, ...] = (),
    ) -> ExternalTurnResult:
        argv = self.build_argv(
            cwd=cwd,
            prompt=prompt,
            session_id=session_id,
            model=model,
            effort=effort,
            new_session_id=new_session_id,
            structured_input=bool(claude_images),
        )
        environment = os.environ.copy()
        input_data = self._prepare_claude_input(
            prompt, claude_images, session_id or new_session_id, claude_sandbox, environment
        )
        if claude_sandbox is not None:
            if self.runtime != "claude" or not self._uses_default_runner:
                raise ProviderUnavailableError(
                    "claude_permission_host_unverified",
                    "Claude file tools require the owned process runner.",
                )
            argv = file_tool_argv(argv, claude_sandbox)
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
            result = self._run_owned_claude_turn(
                argv,
                cwd=cwd,
                environment=environment,
                timeout=timeout,
                expected_session_id=session_id if session_id is not None else new_session_id,
                on_visible_assistant=on_visible_assistant,
                on_process_started=on_claude_process_started,
                sandbox=claude_sandbox,
                input_data=input_data,
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
            event_policy=require_file_tool_event if claude_sandbox is not None else None,
        )

    def _prepare_claude_input(
        self,
        prompt: str,
        images: tuple[VerifiedClaudeImage, ...],
        session_id: str | None,
        sandbox: FileToolSandboxConfig | None,
        environment: dict[str, str],
    ) -> bytes | None:
        if self.runtime == "claude":
            _claude_cpa_environment(environment)
        if not images:
            return None
        if (
            self.runtime != "claude"
            or not self._uses_default_runner
            or sandbox is not None
            or session_id is None
        ):
            raise ProviderUnavailableError(
                "claude_image_input_unverified",
                "Claude image input requires the owned tools-disabled runner.",
            )
        try:
            return encode_claude_image_input(prompt, images, session_id)
        except ClaudeImageInputError:
            raise ProviderUnavailableError(
                "claude_image_input_unverified",
                "Claude image input failed bounded preparation; the turn was not started.",
            ) from None

    def _run_owned_claude_turn(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout: float,
        expected_session_id: str | None,
        on_visible_assistant: VisibleAssistantCallback | None,
        on_process_started: Callable[[], None] | None,
        sandbox: FileToolSandboxConfig | None,
        input_data: bytes | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Own mount descriptors across every productive invocation exit path."""
        if sandbox is not None and input_data is not None:
            raise ProviderUnavailableError(
                "claude_image_input_unverified",
                "Claude image input requires the owned tools-disabled runner.",
            )
        if sandbox is None:
            try:
                receipt = (
                    ClaudeImageReceipt(input_data, expected_session_id=expected_session_id)
                    if input_data is not None
                    else None
                )
            except ClaudeImageInputError:
                raise ProviderUnavailableError(
                    "claude_image_input_unverified",
                    "Claude image input failed bounded preparation.",
                ) from None
            argv = self._verified_claude_argv(
                argv, cwd=cwd, environment=environment, image_input=input_data is not None
            )
            return self._run_claude_process(
                argv,
                cwd=cwd,
                environment=environment,
                timeout=timeout,
                expected_session_id=expected_session_id,
                on_visible_assistant=on_visible_assistant,
                on_process_started=on_process_started,
                input_data=input_data,
                image_receipt=receipt,
            )
        try:
            argv = (str(sandbox.claude_executable), *argv[1:])
            launch = wrap_file_tool_argv(argv, environment, cwd, sandbox)
        except FileToolSandboxError:
            raise ProviderUnavailableError(
                "claude_permission_host_unverified",
                "Claude file-tool isolation could not be verified. The productive turn was not started.",
            ) from None
        with launch:
            verified = self._verified_claude_argv(
                argv, cwd=cwd, environment=environment, file_tools=True
            )
            if verified[0] != argv[0]:
                raise ProviderUnavailableError(
                    "claude_permission_host_unverified",
                    "Claude executable differs from the validated runtime.",
                )
            return self._run_claude_process(
                launch.argv,
                cwd=Path("/"),
                environment=launch.environment,
                timeout=timeout,
                expected_session_id=expected_session_id,
                on_visible_assistant=on_visible_assistant,
                on_process_started=on_process_started,
                event_policy=require_file_tool_event,
                pass_fds=launch.pass_fds,
            )

    def _verified_claude_argv(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        file_tools: bool = False,
        image_input: bool = False,
    ) -> tuple[str, ...]:
        """Bind advertised isolation controls to the executable before invocation."""
        if self._interrupt_requested.is_set():
            raise ExternalTurnInterrupted("claude turn interrupted by user")
        try:
            executable = self._claude_capabilities.require(
                argv[0] if file_tools else self.executable,
                cwd=cwd,
                environment=environment,
                interrupted=self._interrupt_requested,
                file_tools=file_tools,
                image_input=image_input,
            )
        except ClaudeCliUnavailableError:
            raise ProviderUnavailableError(
                "claude_cli_unavailable", "Claude CLI could not be started."
            ) from None
        except ClaudeCliCapabilityError:
            if self._interrupt_requested.is_set():
                raise ExternalTurnInterrupted("claude turn interrupted by user") from None
            raise ProviderUnavailableError(
                "claude_cli_capabilities_unverified",
                "Claude CLI isolation controls could not be verified. The productive turn was not started.",
            ) from None
        return (executable, *argv[1:])

    def _run_claude_process(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout: float,
        expected_session_id: str | None,
        on_visible_assistant: VisibleAssistantCallback | None,
        on_process_started: Callable[[], None] | None = None,
        event_policy: Callable[[dict[str, object]], None] | None = None,
        pass_fds: tuple[int, ...] = (),
        input_data: bytes | None = None,
        image_receipt: ClaudeImageReceipt | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Drain both pipes without communicate() or an unbounded reader queue."""
        if self._interrupt_requested.is_set():
            raise ExternalTurnInterrupted("claude turn interrupted by user")
        if input_data is not None and (
            not isinstance(input_data, bytes) or len(input_data) > MAX_CLAUDE_INPUT_BYTES
        ):
            raise ProviderUnavailableError(
                "claude_image_input_unverified",
                "Claude input exceeds the bounded native input contract.",
            )
        if image_receipt is not None and input_data is None:
            raise ProviderUnavailableError(
                "claude_image_input_unverified", "Claude image receipt has no bounded input."
            )
        reader = ClaudeStreamReader(
            expected_session_id=expected_session_id,
            on_visible_assistant=on_visible_assistant,
            event_policy=event_policy,
            image_receipt=image_receipt,
        )
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=environment,
                stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                close_fds=True,
                pass_fds=pass_fds,
            )
        except OSError as exc:
            raise ProviderUnavailableError(
                "claude_cli_unavailable", "Claude CLI could not be started."
            ) from exc
        with self._process_lock:
            self._active_process = process
        deadline = time.monotonic() + timeout
        diagnostic_bytes = 0
        input_offset = 0
        try:
            if on_process_started is not None:
                try:
                    on_process_started()
                except Exception as error:
                    survived("external_runtime.claude_process_observer", error)
            assert process.stdout is not None and process.stderr is not None
            with selectors.DefaultSelector() as selector:
                for pipe in (process.stdout, process.stderr):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ)
                if process.stdin is not None:
                    if input_data:
                        os.set_blocking(process.stdin.fileno(), False)
                        selector.register(process.stdin, selectors.EVENT_WRITE)
                    else:
                        process.stdin.close()
                while selector.get_map():
                    if self._interrupt_requested.is_set():
                        raise ExternalTurnInterrupted("claude turn interrupted by user")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    for key, _ in selector.select(min(0.1, remaining)):
                        if key.fileobj is process.stdin:
                            assert input_data is not None
                            assert process.stdin is not None
                            try:
                                written = os.write(
                                    key.fd,
                                    memoryview(input_data)[input_offset : input_offset + 65536],
                                )
                            except (BlockingIOError, InterruptedError):
                                continue
                            except BrokenPipeError:
                                raise ClaudeStreamError(
                                    "claude input transfer incomplete"
                                ) from None
                            if written <= 0:
                                raise ClaudeStreamError("claude input transfer incomplete")
                            input_offset += written
                            if input_offset == len(input_data):
                                selector.unregister(key.fileobj)
                                process.stdin.close()
                            continue
                        try:
                            chunk = os.read(key.fd, 65536)
                        except (BlockingIOError, InterruptedError):
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
                while (exit_code := peek_exit_code(process)) is None:
                    if self._interrupt_requested.is_set():
                        raise ExternalTurnInterrupted("claude turn interrupted by user")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    self._interrupt_requested.wait(min(0.05, remaining))
            stdout = reader.finish()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(argv, timeout)
            return subprocess.CompletedProcess(argv, exit_code, stdout, "")
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
                try:
                    self._terminate_claude_process(process, graceful=False)
                finally:
                    for pipe in (process.stdin, process.stdout, process.stderr):
                        if pipe is not None:
                            pipe.close()
            finally:
                with self._process_lock:
                    if self._active_process is process:
                        self._active_process = None

    def _terminate_claude_process(self, process: subprocess.Popen[str], *, graceful: bool) -> None:
        with self._process_lock:
            if process.returncode is not None:
                return
            try:
                os.killpg(process.pid, signal.SIGTERM if graceful else signal.SIGKILL)
            except ProcessLookupError:
                pass
            if graceful:
                # Reserve the leader PID until the last group signal. The
                # stop event can shorten this wait without acquiring the lock.
                deadline = time.monotonic() + 5
                while (
                    peek_exit_code(process) is None
                    and time.monotonic() < deadline
                    and not self._interrupt_requested.is_set()
                ):
                    time.sleep(0.01)
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
        event_policy: Callable[[dict[str, object]], None] | None = None,
    ) -> ExternalTurnResult:
        """Interpret an exited process separately from its lifecycle and stop."""
        if self.runtime == "claude":
            parsed = parse_claude_stream(
                result.stdout,
                expected_session_id=session_id if session_id is not None else new_session_id,
                requested_model=model,
                returncode=result.returncode,
                event_policy=event_policy,
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
