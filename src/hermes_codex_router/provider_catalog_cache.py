from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .provider_catalog import ProviderModel


@dataclass(frozen=True, slots=True)
class CachedProviderModel:
    model_id: str
    label: str
    efforts: tuple[str, ...]
    callback_key: str


@dataclass(frozen=True, slots=True)
class CatalogSnapshot:
    agent_id: str
    models: tuple[CachedProviderModel, ...]
    updated_at: datetime
    source_version: str | None
    last_failure_at: datetime | None


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result if result.tzinfo is not None else result.replace(tzinfo=timezone.utc)


def _key(agent_id: str, model_id: str) -> str:
    return hashlib.sha256(f"{agent_id}\0{model_id}".encode()).hexdigest()[:12]


class ProviderCatalogCache:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def _read(self) -> dict[str, Any]:
        try:
            if self.path.stat().st_mode & 0o077:
                return {"schema_version": 1, "providers": {}}
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": 1, "providers": {}}
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            return {"schema_version": 1, "providers": {}}
        if not isinstance(value.get("providers"), dict):
            return {"schema_version": 1, "providers": {}}
        return value

    def _write(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def load(self, agent_id: str) -> CatalogSnapshot | None:
        raw = self._read().get("providers", {}).get(agent_id)
        if not isinstance(raw, dict):
            return None
        updated_at = _timestamp(raw.get("updated_at"))
        rows = raw.get("models")
        if updated_at is None or not isinstance(rows, list):
            return None
        models: list[CachedProviderModel] = []
        keys: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                return None
            model_id = row.get("model_id")
            label = row.get("label")
            efforts = row.get("efforts")
            callback_key = row.get("callback_key")
            if (
                not isinstance(model_id, str)
                or not isinstance(label, str)
                or not isinstance(efforts, list)
                or not efforts
                or not all(isinstance(item, str) for item in efforts)
                or not isinstance(callback_key, str)
                or callback_key != _key(agent_id, model_id)
                or callback_key in keys
            ):
                return None
            keys.add(callback_key)
            models.append(CachedProviderModel(model_id, label, tuple(efforts), callback_key))
        source_version = raw.get("source_version")
        return CatalogSnapshot(
            agent_id,
            tuple(models),
            updated_at,
            source_version if isinstance(source_version, str) else None,
            _timestamp(raw.get("last_failure_at")),
        )

    def store(
        self,
        agent_id: str,
        models: tuple[ProviderModel, ...],
        *,
        source_version: str | None,
        observed_at: datetime | None = None,
    ) -> CatalogSnapshot:
        now = observed_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        keys: set[str] = set()
        rows: list[dict[str, object]] = []
        for model in models:
            callback_key = _key(agent_id, model.model_id)
            if callback_key in keys:
                raise ValueError("provider catalog callback-key collision")
            keys.add(callback_key)
            rows.append(
                {
                    "model_id": model.model_id,
                    "label": model.label,
                    "efforts": list(model.efforts),
                    "callback_key": callback_key,
                }
            )
        value = self._read()
        providers = value["providers"]
        assert isinstance(providers, dict)
        providers[agent_id] = {
            "updated_at": now.isoformat(),
            "source_version": source_version,
            "last_failure_at": None,
            "models": rows,
        }
        self._write(value)
        result = self.load(agent_id)
        if result is None:
            raise RuntimeError("provider catalog cache verification failed")
        return result

    def mark_failure(self, agent_id: str, *, observed_at: datetime | None = None) -> None:
        value = self._read()
        providers = value["providers"]
        assert isinstance(providers, dict)
        raw = providers.get(agent_id)
        if not isinstance(raw, dict):
            return
        now = observed_at or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        raw["last_failure_at"] = now.isoformat()
        self._write(value)

    def stale_agents(
        self,
        *,
        now: datetime | None = None,
        max_age: timedelta = timedelta(hours=24),
    ) -> tuple[str, ...]:
        evaluated = now or datetime.now(timezone.utc)
        stale: list[str] = []
        providers = self._read().get("providers", {})
        if not isinstance(providers, dict):
            return ()
        for agent_id in providers:
            snapshot = self.load(str(agent_id))
            if (
                snapshot is not None
                and snapshot.last_failure_at is not None
                and evaluated - snapshot.updated_at > max_age
            ):
                stale.append(snapshot.agent_id)
        return tuple(sorted(stale))
