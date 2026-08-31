from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
SUPPORTED_CHECKS = ("status", "accounts", "model_menu", "provider_ping")


class AcceptanceActorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AcceptanceActorConfig:
    api_id: int
    api_hash_file: Path
    session_path: Path
    expected_user_id: int | None
    telegram_chat_id: int
    telegram_thread_id: int
    hub_username: str
    provider_usernames: tuple[str, ...]
    checks: tuple[str, ...]
    timeout_seconds: int
    artifacts_dir: Path


@dataclass(frozen=True, slots=True)
class AcceptanceCheckResult:
    check: str
    target: str
    ok: bool
    response_message_id: int | None
    detail: str


def _private_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise AcceptanceActorError(f"{label} is not a file")
    if path.stat().st_mode & 0o077:
        raise AcceptanceActorError(f"{label} must have mode 0600")


def _absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise AcceptanceActorError(f"{label} must be an absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise AcceptanceActorError(f"{label} must be an absolute path")
    return path.resolve(strict=False)


def _username(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise AcceptanceActorError(f"{label} must be a Telegram username")
    username = value.strip().removeprefix("@")
    if USERNAME.fullmatch(username) is None:
        raise AcceptanceActorError(f"{label} must be a Telegram username")
    return username


def _read_api_hash(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def load_acceptance_actor_config(
    path: Path, *, require_identity: bool = True
) -> AcceptanceActorConfig:
    path = path.expanduser().resolve(strict=False)
    _private_file(path, "acceptance actor config")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceActorError(f"cannot read acceptance actor config: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise AcceptanceActorError("acceptance actor schema_version must be 1")

    api_id = raw.get("api_id")
    expected_user_id = raw.get("expected_user_id")
    chat_id = raw.get("telegram_chat_id")
    thread_id = raw.get("telegram_thread_id")
    timeout = raw.get("timeout_seconds", 30)
    if not isinstance(api_id, int) or api_id <= 0:
        raise AcceptanceActorError("api_id must be a positive integer")
    if expected_user_id is None and not require_identity:
        pass
    elif not isinstance(expected_user_id, int) or expected_user_id <= 0:
        raise AcceptanceActorError("expected_user_id must be a positive integer")
    if not isinstance(chat_id, int) or chat_id >= 0:
        raise AcceptanceActorError("telegram_chat_id must be a negative group id")
    if not isinstance(thread_id, int) or thread_id <= 1:
        raise AcceptanceActorError("telegram_thread_id must select a dedicated forum topic")
    if not isinstance(timeout, int) or not 5 <= timeout <= 180:
        raise AcceptanceActorError("timeout_seconds must be between 5 and 180")

    if "api_hash" in raw or "api_hash_file" in raw:
        raise AcceptanceActorError("store the API hash only in the sibling telegram-api-hash file")
    secret = path.with_name("telegram-api-hash")
    _private_file(secret, "api_hash_file")
    api_hash = _read_api_hash(secret)
    if re.fullmatch(r"[0-9a-fA-F]{32}", api_hash) is None:
        raise AcceptanceActorError("api_hash_file must contain one Telegram API hash")
    session_path = _absolute_path(raw.get("session_path"), "session_path")
    if session_path.exists():
        _private_file(session_path, "session_path")
    if not session_path.parent.is_dir():
        raise AcceptanceActorError("session_path parent must exist")
    artifacts_dir = _absolute_path(raw.get("artifacts_dir"), "artifacts_dir")
    if not artifacts_dir.is_dir():
        raise AcceptanceActorError("artifacts_dir must exist")
    if artifacts_dir.stat().st_mode & 0o077:
        raise AcceptanceActorError("artifacts_dir must have mode 0700")

    raw_providers = raw.get("provider_usernames", [])
    if not isinstance(raw_providers, list):
        raise AcceptanceActorError("provider_usernames must be an array")
    providers = tuple(
        _username(value, f"provider_usernames[{index}]")
        for index, value in enumerate(raw_providers)
    )
    if len(set(name.casefold() for name in providers)) != len(providers):
        raise AcceptanceActorError("provider_usernames contains duplicates")
    raw_checks = raw.get("checks", ["status", "accounts", "model_menu"])
    if (
        not isinstance(raw_checks, list)
        or not raw_checks
        or not all(isinstance(value, str) and value in SUPPORTED_CHECKS for value in raw_checks)
        or len(set(raw_checks)) != len(raw_checks)
    ):
        raise AcceptanceActorError("checks must be a unique non-empty list of supported checks")
    checks = tuple(raw_checks)
    if "provider_ping" in checks and not providers:
        raise AcceptanceActorError("provider_ping requires provider_usernames")

    return AcceptanceActorConfig(
        api_id=api_id,
        api_hash_file=secret,
        session_path=session_path,
        expected_user_id=expected_user_id,
        telegram_chat_id=chat_id,
        telegram_thread_id=thread_id,
        hub_username=_username(raw.get("hub_username"), "hub_username"),
        provider_usernames=providers,
        checks=checks,
        timeout_seconds=timeout,
        artifacts_dir=artifacts_dir,
    )


def _api_hash(config: AcceptanceActorConfig) -> str:
    return _read_api_hash(config.api_hash_file)


def _topic_id(message: Any) -> int | None:
    reply = getattr(message, "reply_to", None)
    if reply is None:
        return None
    top_id = getattr(reply, "reply_to_top_id", None)
    if isinstance(top_id, int):
        return top_id
    reply_id = getattr(reply, "reply_to_msg_id", None)
    return reply_id if isinstance(reply_id, int) else None


async def _wait_for_response(
    client: Any,
    config: AcceptanceActorConfig,
    *,
    after_id: int,
    username: str,
    require_buttons: bool = False,
) -> Any:
    deadline = asyncio.get_running_loop().time() + config.timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        async for message in client.iter_messages(
            config.telegram_chat_id, min_id=after_id, reverse=True
        ):
            if _topic_id(message) != config.telegram_thread_id:
                continue
            sender = await message.get_sender()
            if str(getattr(sender, "username", "")).casefold() != username.casefold():
                continue
            if require_buttons and not getattr(message, "buttons", None):
                continue
            return message
        await asyncio.sleep(0.5)
    raise AcceptanceActorError(f"timed out waiting for @{username}")


async def _run_check(
    client: Any, config: AcceptanceActorConfig, check: str, target: str
) -> AcceptanceCheckResult:
    if check == "provider_ping":
        text = f"@{target} Reply with exactly E2E_OK. This is a connectivity check; use no tools."
        require_buttons = False
    else:
        command = {"status": "status", "accounts": "accounts", "model_menu": "model"}[check]
        text = f"/{command}@{target}"
        require_buttons = check == "model_menu"
    sent = await client.send_message(
        config.telegram_chat_id,
        text,
        reply_to=config.telegram_thread_id,
    )
    try:
        response = await _wait_for_response(
            client,
            config,
            after_id=int(sent.id),
            username=target,
            require_buttons=require_buttons,
        )
    except AcceptanceActorError as exc:
        return AcceptanceCheckResult(check, target, False, None, str(exc))
    response_text = str(getattr(response, "raw_text", "")).strip()
    ok = bool(response_text) or require_buttons
    if check == "provider_ping":
        ok = "E2E_OK" in response_text
    return AcceptanceCheckResult(
        check,
        target,
        ok,
        int(response.id),
        "response received" if ok else "unexpected response",
    )


async def login_acceptance_actor(config: AcceptanceActorConfig) -> dict[str, object]:
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise AcceptanceActorError("install the project with the 'e2e' extra") from exc
    client = TelegramClient(str(config.session_path), config.api_id, _api_hash(config))
    try:
        await client.start()
        identity = await client.get_me()
        user_id = int(identity.id)
        if config.expected_user_id is not None and user_id != config.expected_user_id:
            raise AcceptanceActorError(
                "authorized Telegram account does not match expected_user_id"
            )
    finally:
        await client.disconnect()
    os.chmod(config.session_path, 0o600)
    return {"ok": True, "authorized": True, "user_id": user_id}


async def run_acceptance_checks(config: AcceptanceActorConfig) -> dict[str, object]:
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise AcceptanceActorError("install the project with the 'e2e' extra") from exc
    client = TelegramClient(str(config.session_path), config.api_id, _api_hash(config))
    results: list[AcceptanceCheckResult] = []
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise AcceptanceActorError("acceptance actor is not authorized; run e2e-login")
        identity = await client.get_me()
        if config.expected_user_id is None:
            raise AcceptanceActorError("expected_user_id must be pinned before e2e-run")
        if int(identity.id) != config.expected_user_id:
            raise AcceptanceActorError(
                "authorized Telegram account does not match expected_user_id"
            )
        for check in config.checks:
            targets = (
                config.provider_usernames if check == "provider_ping" else (config.hub_username,)
            )
            for target in targets:
                results.append(await _run_check(client, config, check, target))
    finally:
        await client.disconnect()

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "ok": all(item.ok for item in results),
        "results": [asdict(item) for item in results],
    }
    destination = config.artifacts_dir / f"acceptance-{timestamp}.json"
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return {
        "ok": report["ok"],
        "checks": len(results),
        "passed": sum(item.ok for item in results),
        "artifact": str(destination),
    }
