from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

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


class ExternalCliAdapter:
    def __init__(
        self,
        runtime: str,
        *,
        executable: str | None = None,
        runtime_home: Path | None = None,
        run: Run = subprocess.run,
    ) -> None:
        if runtime not in {"gemini", "antigravity", "opencode"}:
            raise ExternalRuntimeError(f"unsupported external runtime: {runtime}")
        self.runtime = runtime
        self.executable = executable or ("agy" if runtime == "antigravity" else runtime)
        self.runtime_home = runtime_home.expanduser().resolve(strict=True) if runtime_home else None
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
    ) -> tuple[str, ...]:
        canonical_cwd = cwd.expanduser().resolve(strict=True)
        if not prompt.strip():
            raise ExternalRuntimeError("prompt is empty")
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
            selected_model = model
            if model and effort and effort != "default":
                selected_model = f"{model}-{effort}"
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
            if selected_model:
                argv.extend(("--model", selected_model))
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
    ) -> ExternalTurnResult:
        argv = self.build_argv(
            cwd=cwd,
            prompt=prompt,
            session_id=session_id,
            model=model,
            effort=effort,
        )
        environment = os.environ.copy()
        if self.runtime == "gemini" and self.runtime_home is not None:
            environment["GEMINI_CLI_HOME"] = str(self.runtime_home)
        if not interrupt_prepared:
            self.prepare_interruptible_turn()
        if self._uses_default_runner:
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
            with self._process_lock:
                self._active_process = process
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
        if self._interrupt_requested.is_set():
            raise ExternalTurnInterrupted(f"{self.runtime} turn interrupted by user")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:1000]
            if self.runtime == "opencode" and (limit := parse_opencode_limit(detail)):
                raise ProviderLimitError(limit)
            if self.runtime == "antigravity" and (limit := parse_antigravity_limit(detail)):
                raise ProviderLimitError(limit)
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
