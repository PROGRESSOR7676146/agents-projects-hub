from __future__ import annotations

import json
import subprocess
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable

from datetime import timedelta

Run = Callable[..., subprocess.CompletedProcess[str]]

DEFAULT_CATALOG_TTL = timedelta(hours=12)


class ProviderCatalogError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderModel:
    model_id: str
    label: str
    efforts: tuple[str, ...]


ANTIGRAVITY_FALLBACK = (
    ProviderModel("gemini-3.8-flash", "Gemini 3.8 Flash", ("high", "medium", "low")),
    ProviderModel("gemini-3.7-flash", "Gemini 3.7 Flash", ("high", "medium", "low")),
    ProviderModel("gemini-3.6-flash", "Gemini 3.6 Flash", ("high", "medium", "low")),
    ProviderModel("gemini-3.5-flash", "Gemini 3.5 Flash", ("high", "medium", "low")),
    ProviderModel("gemini-3.1-pro", "Gemini 3.1 Pro", ("high", "low")),
    ProviderModel("claude-sonnet-4-6", "Claude Sonnet 4 6", ("default",)),
    ProviderModel("claude-opus-4-6-thinking", "Claude Opus 4 6 Thinking", ("default",)),
    ProviderModel("gpt-oss-120b", "GPT Oss 120b", ("medium",)),
)


def _pretty(value: str) -> str:
    words = value.replace("_", "-").split("-")
    return " ".join(word.upper() if word.lower() == "gpt" else word.title() for word in words)


def _run_lines(argv: tuple[str, ...], run: Run) -> str:
    result = run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:300]
        raise ProviderCatalogError(f"provider model catalog failed safely: {detail}")
    return result.stdout


def opencode_models(executable: str, *, run: Run = subprocess.run) -> tuple[ProviderModel, ...]:
    output = _run_lines((executable, "models", "opencode-go", "--verbose"), run)
    decoder = json.JSONDecoder()
    models: list[ProviderModel] = []
    position = 0
    while position < len(output):
        start = output.find("{", position)
        if start < 0:
            break
        try:
            value, consumed = decoder.raw_decode(output[start:])
        except json.JSONDecodeError:
            position = start + 1
            continue
        position = start + consumed
        if not isinstance(value, dict) or value.get("providerID") != "opencode-go":
            continue
        model_id = value.get("id")
        if not isinstance(model_id, str):
            continue
        name = value.get("name")
        variants = value.get("variants")
        efforts = (
            tuple(str(item) for item in variants)
            if isinstance(variants, dict) and variants
            else ("default",)
        )
        models.append(
            ProviderModel(
                f"opencode-go/{model_id}",
                str(name) if isinstance(name, str) else _pretty(model_id),
                efforts,
            )
        )
    if not models:
        raise ProviderCatalogError("OpenCode returned no usable Go models")
    return tuple(models)


def antigravity_models(executable: str, *, run: Run = subprocess.run) -> tuple[ProviderModel, ...]:
    output = _run_lines((executable, "models"), run)
    grouped: OrderedDict[str, list[str]] = OrderedDict()
    suffixes = ("high", "medium", "low", "max", "minimal")
    for raw in output.splitlines():
        line = raw.strip()
        if not line:
            continue
        if "\t" in line:
            model_id = line.split("\t", 1)[0]
        elif " " in line:
            continue
        else:
            model_id = line
        base = model_id
        effort = "default"
        for candidate in suffixes:
            marker = f"-{candidate}"
            if model_id.endswith(marker):
                base = model_id[: -len(marker)]
                effort = candidate
                break
        grouped.setdefault(base, [])
        if effort not in grouped[base]:
            grouped[base].append(effort)
    models = tuple(
        ProviderModel(model_id, _pretty(model_id), tuple(efforts))
        for model_id, efforts in grouped.items()
    )
    if not models:
        raise ProviderCatalogError("Antigravity returned no usable models")
    return models
