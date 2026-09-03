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
SUPPORTED_CHECKS = (
    "status",
    "accounts",
    "model_menu",
    "provider_ping",
    "reply_route",
    "burst_route",
    "stop_route",
    "forwarded_quote",
    "artifact_delivery",
    "context_contract",
)


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
    provider_agent_ids: tuple[str, ...] = ()


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
    raw_agent_ids = raw.get("provider_agent_ids", [])
    if not isinstance(raw_agent_ids, list) or not all(
        isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", value)
        for value in raw_agent_ids
    ):
        raise AcceptanceActorError("provider_agent_ids must contain safe agent ids")
    provider_agent_ids = tuple(raw_agent_ids)
    if provider_agent_ids and len(provider_agent_ids) != len(providers):
        raise AcceptanceActorError("provider_agent_ids must align with provider_usernames")
    raw_checks = raw.get("checks", ["status", "accounts", "model_menu"])
    if (
        not isinstance(raw_checks, list)
        or not raw_checks
        or not all(isinstance(value, str) and value in SUPPORTED_CHECKS for value in raw_checks)
        or len(set(raw_checks)) != len(raw_checks)
    ):
        raise AcceptanceActorError("checks must be a unique non-empty list of supported checks")
    checks = tuple(raw_checks)
    if (
        any(
            check
            in {
                "provider_ping",
                "reply_route",
                "burst_route",
                "stop_route",
                "forwarded_quote",
                "artifact_delivery",
                "context_contract",
            }
            for check in checks
        )
        and not providers
    ):
        raise AcceptanceActorError("provider checks require provider_usernames")
    if "stop_route" in checks and (
        "model_menu" not in checks or checks.index("model_menu") > checks.index("stop_route")
    ):
        raise AcceptanceActorError("model_menu must run before stop_route")
    if "context_contract" in checks and (len(providers) < 2 or len(provider_agent_ids) < 2):
        raise AcceptanceActorError("context_contract requires two aligned providers and agent ids")

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
        provider_agent_ids=provider_agent_ids,
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


def _allowed_canary_sender(sender: Any, config: AcceptanceActorConfig) -> bool:
    sender_id = getattr(sender, "id", None)
    if sender_id == config.expected_user_id:
        return True
    username = str(getattr(sender, "username", "")).casefold()
    allowed_usernames = {
        config.hub_username.casefold(),
        *(value.casefold() for value in config.provider_usernames),
    }
    return username in allowed_usernames


async def _wait_for_response(
    client: Any,
    config: AcceptanceActorConfig,
    *,
    after_id: int,
    username: str,
    require_buttons: bool = False,
    require_document: bool = False,
    timeout_seconds: int | None = None,
) -> Any:
    deadline = asyncio.get_running_loop().time() + (
        config.timeout_seconds if timeout_seconds is None else timeout_seconds
    )
    while asyncio.get_running_loop().time() < deadline:
        async for message in client.iter_messages(
            config.telegram_chat_id, min_id=after_id, reverse=True
        ):
            if _topic_id(message) != config.telegram_thread_id:
                continue
            sender = await message.get_sender()
            sender_username = str(getattr(sender, "username", "")).casefold()
            if sender_username == username.casefold():
                if require_buttons and not getattr(message, "buttons", None):
                    continue
                if require_document and getattr(message, "document", None) is None:
                    continue
                return message
            if not _allowed_canary_sender(sender, config):
                raise AcceptanceActorError(
                    "canary topic received unrelated traffic during acceptance"
                )
        await asyncio.sleep(0.5)
    raise AcceptanceActorError(f"timed out waiting for @{username}")


async def _click_callback_prefix(message: Any, prefix: bytes) -> None:
    for row in getattr(message, "buttons", None) or ():
        for button in row:
            data = getattr(button, "data", None)
            if isinstance(data, bytes) and data.startswith(prefix):
                await button.click()
                return
    raise AcceptanceActorError(
        f"model menu has no {prefix.decode('ascii', errors='replace')} callback"
    )


async def _click_callback_exact(message: Any, data: bytes) -> None:
    for row in getattr(message, "buttons", None) or ():
        for button in row:
            if getattr(button, "data", None) == data:
                await button.click()
                return
    raise AcceptanceActorError(
        f"model menu has no {data.decode('ascii', errors='replace')} callback"
    )


async def _complete_model_selection(
    client: Any,
    config: AcceptanceActorConfig,
    menu: Any,
) -> Any:
    current = menu
    for prefix in (b"provider:", b"choose:", b"use:"):
        await _click_callback_prefix(current, prefix)
        current = await _wait_for_response(
            client,
            config,
            after_id=int(current.id),
            username=config.hub_username,
            require_buttons=prefix != b"use:",
        )
    return current


async def _select_provider(client: Any, config: AcceptanceActorConfig, agent_id: str) -> Any:
    request = await client.send_message(
        config.telegram_chat_id,
        f"/model@{config.hub_username}",
        reply_to=config.telegram_thread_id,
    )
    menu = await _wait_for_response(
        client,
        config,
        after_id=int(request.id),
        username=config.hub_username,
        require_buttons=True,
    )
    await _click_callback_exact(menu, f"provider:{agent_id}".encode())
    models = await _wait_for_response(
        client,
        config,
        after_id=int(menu.id),
        username=config.hub_username,
        require_buttons=True,
    )
    await _click_callback_prefix(models, b"choose:")
    efforts = await _wait_for_response(
        client,
        config,
        after_id=int(models.id),
        username=config.hub_username,
        require_buttons=True,
    )
    await _click_callback_prefix(efforts, b"use:")
    return await _wait_for_response(
        client,
        config,
        after_id=int(efforts.id),
        username=config.hub_username,
    )


async def _run_check(
    client: Any, config: AcceptanceActorConfig, check: str, target: str
) -> AcceptanceCheckResult:
    if check == "context_contract":
        source_username, target_username = config.provider_usernames[:2]
        source_agent_id, target_agent_id = config.provider_agent_ids[:2]
        try:
            await _select_provider(client, config, source_agent_id)
            source = await client.send_message(
                config.telegram_chat_id,
                f"@{source_username} Reply exactly CONTEXT_SOURCE_E2E_7391. Use no tools.",
                reply_to=config.telegram_thread_id,
            )
            source_reply = await _wait_for_response(
                client, config, after_id=int(source.id), username=source_username
            )
            if "CONTEXT_SOURCE_E2E_7391" not in str(source_reply.raw_text):
                raise AcceptanceActorError("source marker was not returned")

            applied = await _select_provider(client, config, target_agent_id)
            if "No prior agent history was injected" not in str(applied.raw_text):
                raise AcceptanceActorError("model switch did not confirm context isolation")

            isolated = await client.send_message(
                config.telegram_chat_id,
                "Reply exactly CONTEXT_SWITCH_ISOLATED_OK. Use no tools.",
                reply_to=config.telegram_thread_id,
            )
            isolated_reply = await _wait_for_response(
                client, config, after_id=int(isolated.id), username=target_username
            )
            if "CONTEXT_SWITCH_ISOLATED_OK" not in str(isolated_reply.raw_text):
                raise AcceptanceActorError("switched provider did not respond in isolation")

            context = await client.send_message(
                config.telegram_chat_id,
                f"/context@{config.hub_username} {source_agent_id} 8",
                reply_to=config.telegram_thread_id,
            )
            context_reply = await _wait_for_response(
                client, config, after_id=int(context.id), username=target_username
            )
            if "CONTEXT_SOURCE_E2E_7391" not in str(context_reply.raw_text):
                raise AcceptanceActorError("explicit context did not contain the source marker")
        except AcceptanceActorError as exc:
            return AcceptanceCheckResult(check, target, False, None, str(exc))
        return AcceptanceCheckResult(
            check,
            target,
            True,
            int(context_reply.id),
            "switch isolation and explicit context verified",
        )
    if check == "artifact_delivery":
        filename = "hub-artifact-e2e.md"
        expected = b"HUB_ARTIFACT_E2E_OK\n"
        sent = await client.send_message(
            config.telegram_chat_id,
            (
                f"@{target} Create {filename} in the exact Hub artifact delivery directory "
                "for this turn. Its complete UTF-8 content must be HUB_ARTIFACT_E2E_OK "
                "followed by one newline. Reply briefly; do no other work."
            ),
            reply_to=config.telegram_thread_id,
        )
        try:
            response = await _wait_for_response(
                client,
                config,
                after_id=int(sent.id),
                username=target,
                require_document=True,
            )
            received_name = str(getattr(getattr(response, "file", None), "name", ""))
            payload = await response.download_media(file=bytes)
        except AcceptanceActorError as exc:
            return AcceptanceCheckResult(check, target, False, None, str(exc))
        ok = received_name == filename and payload == expected
        return AcceptanceCheckResult(
            check,
            target,
            ok,
            int(response.id),
            "document filename and content verified" if ok else "unexpected document",
        )
    if check == "forwarded_quote":
        sent = await client.send_message(
            config.telegram_chat_id,
            f"@{target} Reply with exactly FORWARD_SOURCE_OK. Use no tools.",
            reply_to=config.telegram_thread_id,
        )
        try:
            source = await _wait_for_response(
                client, config, after_id=int(sent.id), username=target
            )
            if "FORWARD_SOURCE_OK" not in str(getattr(source, "raw_text", "")):
                return AcceptanceCheckResult(
                    check, target, False, int(source.id), "unexpected source response"
                )
            forwarded_id = await _forward_to_topic(client, config, source)
            try:
                unexpected = await _wait_for_response(
                    client,
                    config,
                    after_id=forwarded_id,
                    username=target,
                    timeout_seconds=5,
                )
            except AcceptanceActorError:
                unexpected = None
            if unexpected is not None:
                return AcceptanceCheckResult(
                    check,
                    target,
                    False,
                    int(unexpected.id),
                    "provider answered a passive forward",
                )
            follow_up = await client.send_message(
                config.telegram_chat_id,
                (
                    f"@{target} Reply with exactly FORWARD_CONTEXT_OK if the immediately "
                    "preceding forwarded message was shown only as quoted context. Use no tools."
                ),
                reply_to=config.telegram_thread_id,
            )
            response = await _wait_for_response(
                client, config, after_id=int(follow_up.id), username=target
            )
        except AcceptanceActorError as exc:
            return AcceptanceCheckResult(check, target, False, None, str(exc))
        ok = "FORWARD_CONTEXT_OK" in str(getattr(response, "raw_text", ""))
        return AcceptanceCheckResult(
            check,
            target,
            ok,
            int(response.id),
            "response received" if ok else "forward was not visible as quoted context",
        )
    if check == "burst_route":
        sent = []
        for text in (
            f"@{target} Reply with exactly",
            "BURST_E2E_OK",
            "after reading all three messages together. Use no tools.",
        ):
            sent.append(
                await client.send_message(
                    config.telegram_chat_id,
                    text,
                    reply_to=config.telegram_thread_id,
                )
            )
        try:
            response = await _wait_for_response(
                client,
                config,
                after_id=max(int(message.id) for message in sent),
                username=target,
            )
        except AcceptanceActorError as exc:
            return AcceptanceCheckResult(check, target, False, None, str(exc))
        response_text = str(getattr(response, "raw_text", "")).strip()
        ok = "BURST_E2E_OK" in response_text
        return AcceptanceCheckResult(
            check,
            target,
            ok,
            int(response.id),
            "response received" if ok else "unexpected response",
        )
    if check == "stop_route":
        await client.send_message(
            config.telegram_chat_id,
            f"@{target} Run the harmless command `sleep 60`, then reply STOP_TOO_LATE.",
            reply_to=config.telegram_thread_id,
        )
        await asyncio.sleep(3)
        stopped = await client.send_message(
            config.telegram_chat_id,
            "stop",
            reply_to=config.telegram_thread_id,
        )
        try:
            stop_response = await _wait_for_response(
                client,
                config,
                after_id=int(stopped.id),
                username=config.hub_username,
            )
        except AcceptanceActorError as exc:
            return AcceptanceCheckResult(check, target, False, None, str(exc))
        stop_text = str(getattr(stop_response, "raw_text", "")).strip()
        recovery = await client.send_message(
            config.telegram_chat_id,
            f"@{target} Reply with exactly AFTER_STOP_E2E_OK. Use no tools.",
            reply_to=config.telegram_thread_id,
        )
        try:
            response = await _wait_for_response(
                client,
                config,
                after_id=int(recovery.id),
                username=target,
            )
        except AcceptanceActorError as exc:
            return AcceptanceCheckResult(check, target, False, None, str(exc))
        response_text = str(getattr(response, "raw_text", "")).strip()
        ok = "Останавливаю активную работу" in stop_text and "AFTER_STOP_E2E_OK" in response_text
        return AcceptanceCheckResult(
            check,
            target,
            ok,
            int(response.id),
            "response received" if ok else "unexpected response",
        )
    if check == "provider_ping":
        text = f"@{target} Reply with exactly E2E_OK. This is a connectivity check; use no tools."
        require_buttons = False
    elif check == "reply_route":
        text = (
            f"@{target} Reply with exactly REPLY_PARENT_OK. "
            "This is a reply-routing check; use no tools."
        )
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
    if check == "model_menu":
        try:
            response = await _complete_model_selection(client, config, response)
        except AcceptanceActorError as exc:
            return AcceptanceCheckResult(check, target, False, None, str(exc))
        response_text = str(getattr(response, "raw_text", "")).strip()
        ok = "will start on the next message" in response_text or "already active" in response_text
    if check == "reply_route":
        if "REPLY_PARENT_OK" not in response_text:
            ok = False
        else:
            follow_up = await client.send_message(
                config.telegram_chat_id,
                "Reply with exactly REPLY_CHILD_OK. Use no tools.",
                reply_to=int(response.id),
            )
            try:
                response = await _wait_for_response(
                    client,
                    config,
                    after_id=int(follow_up.id),
                    username=target,
                )
            except AcceptanceActorError as exc:
                return AcceptanceCheckResult(check, target, False, None, str(exc))
            response_text = str(getattr(response, "raw_text", "")).strip()
            ok = "REPLY_CHILD_OK" in response_text
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
    # Telethon's generated sync/async overloads vary across releases; this
    # module intentionally uses the runtime async API throughout.
    client: Any = TelegramClient(str(config.session_path), config.api_id, _api_hash(config))
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


async def _run_configured_checks(
    client: Any, config: AcceptanceActorConfig
) -> list[AcceptanceCheckResult]:
    results: list[AcceptanceCheckResult] = []
    for check in config.checks:
        targets = _targets_for_check(config, check)
        for target in targets:
            result = await _run_check(client, config, check, target)
            results.append(result)
            if not result.ok:
                return results
    return results


async def run_acceptance_checks(config: AcceptanceActorConfig) -> dict[str, object]:
    try:
        from telethon import TelegramClient
    except ImportError as exc:
        raise AcceptanceActorError("install the project with the 'e2e' extra") from exc
    client: Any = TelegramClient(str(config.session_path), config.api_id, _api_hash(config))
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
        results = await _run_configured_checks(client, config)
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


def _targets_for_check(config: AcceptanceActorConfig, check: str) -> tuple[str, ...]:
    if check in {"stop_route", "artifact_delivery"}:
        return config.provider_usernames[:1]
    if check in {"provider_ping", "reply_route", "burst_route", "forwarded_quote"}:
        return config.provider_usernames
    return (config.hub_username,)


async def _forward_to_topic(client: Any, config: AcceptanceActorConfig, source: Any) -> int:
    try:
        from telethon import functions, helpers
    except ImportError as exc:
        raise AcceptanceActorError("install the project with the 'e2e' extra") from exc
    peer = await client.get_input_entity(config.telegram_chat_id)
    result = await client(
        functions.messages.ForwardMessagesRequest(
            from_peer=peer,
            id=[int(source.id)],
            to_peer=peer,
            random_id=[helpers.generate_random_long()],
            top_msg_id=config.telegram_thread_id,
        )
    )
    message_ids = [
        int(update.message.id)
        for update in getattr(result, "updates", ())
        if getattr(update, "message", None) is not None
    ]
    if not message_ids:
        raise AcceptanceActorError("Telegram did not confirm the forwarded message")
    return max(message_ids)
