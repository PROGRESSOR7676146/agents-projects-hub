from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

Run = Callable[..., subprocess.CompletedProcess[str]]


class ExternalRuntimeError(RuntimeError):
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
        if runtime not in {"gemini", "opencode"}:
            raise ExternalRuntimeError(f"unsupported external runtime: {runtime}")
        self.runtime = runtime
        self.executable = executable or runtime
        self.runtime_home = runtime_home.expanduser().resolve(strict=True) if runtime_home else None
        self._run = run

    def build_argv(
        self,
        *,
        cwd: Path,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
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
        argv.append(prompt)
        return tuple(argv)

    def run_turn(
        self,
        *,
        cwd: Path,
        prompt: str,
        session_id: str | None = None,
        model: str | None = None,
        timeout: float = 900,
    ) -> ExternalTurnResult:
        argv = self.build_argv(
            cwd=cwd,
            prompt=prompt,
            session_id=session_id,
            model=model,
        )
        environment = os.environ.copy()
        if self.runtime == "gemini" and self.runtime_home is not None:
            environment["GEMINI_CLI_HOME"] = str(self.runtime_home)
        result = self._run(
            argv,
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:1000]
            raise ExternalRuntimeError(f"{self.runtime} failed safely: {detail}")
        values = _json_values(result.stdout)
        if not values:
            raise ExternalRuntimeError(f"{self.runtime} returned no structured output")
        provider_session_id: str | None = session_id
        detected_model: str | None = model
        text_parts: list[str] = []
        for value in values:
            for key in ("session_id", "sessionId", "sessionID"):
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
