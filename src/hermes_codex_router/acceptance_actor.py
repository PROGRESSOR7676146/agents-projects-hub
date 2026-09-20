from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .acceptance_runtime import (
    AcceptanceRuntimeError,
    FixedServiceSupervisor,
    ReadOnlyAcceptanceState,
    ServiceSnapshot,
)

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
    "codex_interaction_v2",
    "p0_p1_live",
)

P0_P1_CHECKS = (
    "caption_only_document_provider_content",
    "album_provider_content",
    "attachment_during_active_turn_fifo",
    "restart_idempotency_and_recovery",
    "oversize_explicit_unavailable_notice",
    "p1_live_turn_context_and_quota_labels",
    "p1_status_context_and_accounts_read_only",
)
_MAX_CLOUD_BOT_FILE_BYTES = 20 * 1024 * 1024


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
    state_path: Path | None = None
    allow_service_restart: bool = False


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
    if not isinstance(timeout, int) or not 5 <= timeout <= 600:
        raise AcceptanceActorError("timeout_seconds must be between 5 and 600")

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
    raw_state_path = raw.get("state_path")
    state_path = None
    if raw_state_path is not None:
        state_path = _absolute_path(raw_state_path, "state_path")
        _private_file(state_path, "state_path")
    allow_service_restart = raw.get("allow_service_restart", False)
    if not isinstance(allow_service_restart, bool):
        raise AcceptanceActorError("allow_service_restart must be a boolean")

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
                "codex_interaction_v2",
                "p0_p1_live",
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
    if "codex_interaction_v2" in checks and (
        len(provider_agent_ids) != len(providers) or provider_agent_ids.count("codex") != 1
    ):
        raise AcceptanceActorError(
            "codex_interaction_v2 requires one aligned codex provider identity"
        )
    if "p0_p1_live" in checks:
        if len(provider_agent_ids) != len(providers) or provider_agent_ids.count("codex") != 1:
            raise AcceptanceActorError("p0_p1_live requires one aligned codex provider identity")
        if state_path is None:
            raise AcceptanceActorError("p0_p1_live requires a private state_path")
        if not allow_service_restart:
            raise AcceptanceActorError("p0_p1_live requires explicit service restart opt-in")
        if timeout < 120:
            raise AcceptanceActorError("p0_p1_live requires timeout_seconds of at least 120")

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
        state_path=state_path,
        allow_service_restart=allow_service_restart,
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


async def _wait_for_job(
    state: ReadOnlyAcceptanceState,
    chat_id: int,
    message_id: int,
    statuses: set[str],
    *,
    timeout_seconds: int = 60,
) -> list[tuple[str, str]]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    latest: list[tuple[str, str]] = []
    while asyncio.get_running_loop().time() < deadline:
        latest = state.jobs_for_input(chat_id, message_id)
        if len(latest) == 1 and latest[0][1] in statuses:
            return latest
        await asyncio.sleep(0.25)
    raise AcceptanceActorError(f"durable job did not reach {sorted(statuses)}; count={len(latest)}")


async def _wait_for_markers(
    client: Any,
    config: AcceptanceActorConfig,
    *,
    after_id: int,
    username: str,
    markers: tuple[str, ...],
) -> Any:
    deadline = asyncio.get_running_loop().time() + config.timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        async for message in client.iter_messages(
            config.telegram_chat_id, min_id=after_id, reverse=True
        ):
            if _topic_id(message) != config.telegram_thread_id:
                continue
            sender = await message.get_sender()
            sender_username = str(getattr(sender, "username", "")).casefold()
            if sender_username == username.casefold():
                text = str(getattr(message, "raw_text", ""))
                if all(marker in text for marker in markers):
                    return message
                if any(
                    phrase in text
                    for phrase in (
                        "Project root validation failed",
                        "did not start: the project binding is invalid",
                        "Incoming material integrity validation failed",
                    )
                ):
                    raise AcceptanceActorError("provider returned a pre-execution failure")
                continue
            if not _allowed_canary_sender(sender, config):
                raise AcceptanceActorError(
                    "canary topic received unrelated traffic during acceptance"
                )
        await asyncio.sleep(0.5)
    raise AcceptanceActorError(f"timed out waiting for verified response from @{username}")


async def _send_input_document(
    client: Any,
    config: AcceptanceActorConfig,
    path: Path,
    caption: str,
) -> Any:
    return await client.send_file(
        config.telegram_chat_id,
        str(path),
        caption=caption,
        reply_to=config.telegram_thread_id,
        force_document=True,
    )


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
            value = getattr(button, "data", None)
            # Click the original callback unchanged: the Controller still
            # validates the generation suffix before changing session state.
            if isinstance(value, bytes) and value.split(b"~", 1)[0] == data:
                await button.click()
                return
    raise AcceptanceActorError(
        f"model menu has no {data.decode('ascii', errors='replace')} callback"
    )


def _stop_acknowledged(text: str) -> bool:
    if "Останавливаю активную работу" in text:
        return True
    return (
        "Активной работы нет" in text
        and re.search(r"отменено задач в очереди: [1-9][0-9]*", text) is not None
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


async def _run_p0_p1_live_checks(
    client: Any,
    config: AcceptanceActorConfig,
    target: str,
) -> list[AcceptanceCheckResult]:
    if config.state_path is None or not config.allow_service_restart:
        raise AcceptanceActorError("p0_p1_live is missing its local state or restart authority")
    state = ReadOnlyAcceptanceState(config.state_path)
    services = FixedServiceSupervisor()
    results: list[AcceptanceCheckResult] = []
    current_check = P0_P1_CHECKS[0]
    failure: tuple[str, str] | None = None
    nonce = uuid.uuid4().hex[:12].upper()
    initial_services: ServiceSnapshot | None = None

    def passed(check: str, response_id: int | None, detail: str) -> None:
        results.append(AcceptanceCheckResult(check, target, True, response_id, detail))

    try:
        initial_services = services.capture_active_state()
        if not initial_services.all_active:
            raise AcceptanceActorError(
                "p0_p1_live requires the Controller and Codex worker to be active"
            )
        await _select_provider(client, config, "codex")
        with tempfile.TemporaryDirectory(prefix="hub-live-inbound-") as directory:
            root = Path(directory)
            caption_token = f"CAPTION_CONTENT_{nonce}"
            album_one = f"ALBUM_ONE_{nonce}"
            album_two = f"ALBUM_TWO_{nonce}"
            late_token = f"LATE_CONTENT_{nonce}"
            recovery_token = f"RECOVERY_CONTENT_{nonce}"
            paths = {
                "caption": root / "caption-only.txt",
                "album_one": root / "album-one.txt",
                "album_two": root / "album-two.txt",
                "late": root / "late.txt",
                "recovery": root / "recovery.txt",
                "oversize": root / "oversize.txt",
            }
            paths["caption"].write_text(caption_token + "\n", encoding="utf-8")
            paths["album_one"].write_text(album_one + "\n", encoding="utf-8")
            paths["album_two"].write_text(album_two + "\n", encoding="utf-8")
            paths["late"].write_text(late_token + "\n", encoding="utf-8")
            paths["recovery"].write_text(recovery_token + "\n", encoding="utf-8")
            with paths["oversize"].open("wb") as stream:
                stream.seek(_MAX_CLOUD_BOT_FILE_BYTES)
                stream.write(b"\n")

            current_check = P0_P1_CHECKS[0]
            caption_message = await _send_input_document(
                client,
                config,
                paths["caption"],
                f"@{target} Read the attached UTF-8 document. Reply with CAPTION_FILE_OK "
                "followed by the exact token found only inside the file. Use no tools.",
            )
            caption_response = await _wait_for_markers(
                client,
                config,
                after_id=int(caption_message.id),
                username=target,
                markers=("CAPTION_FILE_OK", caption_token),
            )
            caption_jobs = await _wait_for_job(
                state,
                config.telegram_chat_id,
                int(caption_message.id),
                {"completed"},
            )
            passed(
                current_check,
                int(caption_response.id),
                f"provider content and {len(caption_jobs)} durable job verified",
            )

            current_check = P0_P1_CHECKS[1]
            album_caption = (
                f"@{target} Read both attached UTF-8 documents. Reply with ALBUM_FILE_OK "
                "followed by both exact tokens found only inside the files, in order. "
                "Use no tools."
            )
            album_messages = await client.send_file(
                config.telegram_chat_id,
                [str(paths["album_one"]), str(paths["album_two"])],
                caption=[album_caption, ""],
                reply_to=config.telegram_thread_id,
                force_document=True,
            )
            if not isinstance(album_messages, list) or len(album_messages) != 2:
                raise AcceptanceActorError("Telegram did not create a two-part album")
            grouped = {getattr(message, "grouped_id", None) for message in album_messages}
            if len(grouped) != 1 or None in grouped:
                raise AcceptanceActorError("Telegram document upload was not one album")
            album_response = await _wait_for_markers(
                client,
                config,
                after_id=min(int(message.id) for message in album_messages),
                username=target,
                markers=("ALBUM_FILE_OK", album_one, album_two),
            )
            album_job_rows: list[tuple[str, str]] = []
            for message in album_messages:
                album_job_rows.extend(
                    await _wait_for_job(
                        state,
                        config.telegram_chat_id,
                        int(message.id),
                        {"completed"},
                    )
                )
            album_job_ids = {row[0] for row in album_job_rows}
            if len(album_job_rows) != 2 or len(album_job_ids) != 1:
                raise AcceptanceActorError("album parts did not bind to one durable job")
            if state.material_count(next(iter(album_job_ids))) != 2:
                raise AcceptanceActorError("album job did not retain both material snapshots")
            passed(current_check, int(album_response.id), "one job and two materials verified")

            current_check = P0_P1_CHECKS[2]
            active_marker = f"ACTIVE_FIRST_{nonce}"
            active = await client.send_message(
                config.telegram_chat_id,
                f"@{target} Run the harmless shell command sleep 12, then reply exactly "
                f"{active_marker}.",
                reply_to=config.telegram_thread_id,
            )
            await _wait_for_job(
                state,
                config.telegram_chat_id,
                int(active.id),
                {"executing"},
                timeout_seconds=45,
            )
            late = await _send_input_document(
                client,
                config,
                paths["late"],
                f"@{target} Read the attached UTF-8 document after the active turn. "
                "Reply with LATE_FILE_OK followed by the exact token found only inside it. "
                "Use no tools.",
            )
            await _wait_for_job(
                state,
                config.telegram_chat_id,
                int(late.id),
                {"queued", "leased", "executing", "result_ready", "completed"},
            )
            active_response = await _wait_for_markers(
                client,
                config,
                after_id=int(active.id),
                username=target,
                markers=(active_marker,),
            )
            late_response = await _wait_for_markers(
                client,
                config,
                after_id=int(late.id),
                username=target,
                markers=("LATE_FILE_OK", late_token),
            )
            late_jobs = await _wait_for_job(
                state,
                config.telegram_chat_id,
                int(late.id),
                {"completed"},
            )
            if len(late_jobs) != 1:
                raise AcceptanceActorError("late material did not retain one FIFO job")
            passed(
                current_check,
                int(late_response.id),
                f"active response {int(active_response.id)} preceded one late FIFO job",
            )

            current_check = P0_P1_CHECKS[3]
            services.stop_codex_worker()
            if services.is_codex_worker_active():
                raise AcceptanceActorError("Codex worker did not stop for recovery check")
            recovery = await _send_input_document(
                client,
                config,
                paths["recovery"],
                f"@{target} Read the attached UTF-8 document after recovery. Reply with "
                "RECOVERY_FILE_OK followed by the exact token found only inside it. "
                "Use no tools.",
            )
            queued = await _wait_for_job(
                state,
                config.telegram_chat_id,
                int(recovery.id),
                {"queued"},
            )
            recovery_job_id = queued[0][0]
            services.restart_controller()
            if not services.is_controller_active():
                raise AcceptanceActorError("Controller did not restart")
            if len(state.jobs_for_input(config.telegram_chat_id, int(recovery.id))) != 1:
                raise AcceptanceActorError("Controller restart duplicated durable admission")
            services.start_codex_worker()
            if not services.is_codex_worker_active():
                raise AcceptanceActorError("Codex worker did not recover")
            recovery_response = await _wait_for_markers(
                client,
                config,
                after_id=int(recovery.id),
                username=target,
                markers=("RECOVERY_FILE_OK", recovery_token),
            )
            recovery_jobs = await _wait_for_job(
                state,
                config.telegram_chat_id,
                int(recovery.id),
                {"completed"},
            )
            if len(recovery_jobs) != 1 or state.material_count(recovery_job_id) != 1:
                raise AcceptanceActorError("recovery produced duplicate job or material rows")
            passed(current_check, int(recovery_response.id), "restart recovery stayed exactly once")

            current_check = P0_P1_CHECKS[4]
            oversize_ack = f"OVERSIZE_ACK_{nonce}"
            oversize = await _send_input_document(
                client,
                config,
                paths["oversize"],
                f"@{target} Do not claim to read unavailable content. Reply exactly "
                f"{oversize_ack}.",
            )
            oversize_response = await _wait_for_markers(
                client,
                config,
                after_id=int(oversize.id),
                username=target,
                markers=(oversize_ack, "exceeds the 20 MB cloud Bot API download limit"),
            )
            passed(current_check, int(oversize_response.id), "explicit 20 MB notice verified")

            current_check = P0_P1_CHECKS[5]
            p1_marker = f"P1_LIVE_{nonce}"
            p1_request = await client.send_message(
                config.telegram_chat_id,
                f"@{target} Reply exactly {p1_marker}. Use no tools.",
                reply_to=config.telegram_thread_id,
            )
            p1_response = await _wait_for_markers(
                client,
                config,
                after_id=int(p1_request.id),
                username=target,
                markers=(p1_marker,),
            )
            p1_text = str(getattr(p1_response, "raw_text", ""))
            if re.search(r"Context remaining: [0-9]+(?:\.[0-9]+)?%", p1_text) is None:
                raise AcceptanceActorError("live Codex response has no numeric context percentage")
            quota_labels = re.findall(
                r"(?:Primary window|Secondary window|Weekly|\d+-(?:minute|hour|day)) remaining:",
                p1_text,
            )
            if not quota_labels:
                raise AcceptanceActorError("live Codex response has no provider quota label")
            passed(current_check, int(p1_response.id), "numeric context and quota label verified")

            current_check = P0_P1_CHECKS[6]
            status_request = await client.send_message(
                config.telegram_chat_id,
                f"/status@{config.hub_username}",
                reply_to=config.telegram_thread_id,
            )
            status_response = await _wait_for_markers(
                client,
                config,
                after_id=int(status_request.id),
                username=config.hub_username,
                markers=("Context",),
            )
            accounts_request = await client.send_message(
                config.telegram_chat_id,
                f"/accounts@{config.hub_username}",
                reply_to=config.telegram_thread_id,
            )
            await _wait_for_markers(
                client,
                config,
                after_id=int(accounts_request.id),
                username=config.hub_username,
                markers=("Codex",),
            )
            passed(current_check, int(status_response.id), "read-only status and accounts verified")
    except (AcceptanceActorError, AcceptanceRuntimeError) as exc:
        failure = (current_check, str(exc))
    except OSError as exc:
        failure = (
            current_check,
            f"bounded local acceptance operation failed: {type(exc).__name__}",
        )
    finally:
        if initial_services is not None:
            try:
                services.restore(initial_services)
            except AcceptanceRuntimeError as exc:
                if failure is None:
                    failure = (P0_P1_CHECKS[3], str(exc))
                else:
                    failed_check, detail = failure
                    failure = (failed_check, f"{detail}; service restoration failed: {exc}")

    if failure is not None:
        failed_check, detail = failure
        replacement = AcceptanceCheckResult(failed_check, target, False, None, detail)
        for index, result in enumerate(results):
            if result.check == failed_check:
                results[index] = replacement
                break
        else:
            results.append(replacement)
    return results


async def _run_check(
    client: Any, config: AcceptanceActorConfig, check: str, target: str
) -> AcceptanceCheckResult:
    if check == "codex_interaction_v2":
        try:
            await _select_provider(client, config, "codex")
            short_request = await client.send_message(
                config.telegram_chat_id,
                f"@{target} What is 2 + 2? Answer for a phone in one short sentence. Use no tools.",
                reply_to=config.telegram_thread_id,
            )
            short_response = await _wait_for_response(
                client, config, after_id=int(short_request.id), username=target
            )
            short_text = str(getattr(short_response, "raw_text", "")).strip()
            if "4" not in short_text or len(short_text) > 400:
                return AcceptanceCheckResult(
                    check,
                    target,
                    False,
                    int(short_response.id),
                    "short task response was empty, incorrect, or not bounded",
                )

            ambiguous_request = await client.send_message(
                config.telegram_chat_id,
                (
                    f"@{target} Without tools or file changes, prepare the fictional launch "
                    "note. The request is intentionally underspecified: no audience, facts, "
                    "format, or language are given."
                ),
                reply_to=config.telegram_thread_id,
            )
            ambiguous_response = await _wait_for_response(
                client, config, after_id=int(ambiguous_request.id), username=target
            )
            ambiguous_text = str(getattr(ambiguous_response, "raw_text", "")).strip()
            question_count = ambiguous_text.count("?")
            if (
                not 1 <= question_count <= 2
                or len(ambiguous_text) > 800
                or getattr(ambiguous_response, "document", None) is not None
            ):
                return AcceptanceCheckResult(
                    check,
                    target,
                    False,
                    int(ambiguous_response.id),
                    "ambiguous task did not produce a bounded focused clarification",
                )

            progress_request = await client.send_message(
                config.telegram_chat_id,
                (
                    f"@{target} Use no tools. Compare three fictional options: A is fast but "
                    "irreversible, B is slower and reversible, C is untested. Start with one "
                    "brief line labelled Approach, then continue immediately with a concise "
                    "recommendation labelled Recommendation."
                ),
                reply_to=config.telegram_thread_id,
            )
            progress_response = await _wait_for_response(
                client, config, after_id=int(progress_request.id), username=target
            )
            progress_text = str(getattr(progress_response, "raw_text", "")).strip()
            lowered = progress_text.casefold()
            approach_at = lowered.find("approach")
            recommendation_at = lowered.find("recommendation")
            if (
                approach_at < 0
                or recommendation_at <= approach_at
                or len(progress_text) > 2_000
                or getattr(progress_response, "document", None) is not None
            ):
                return AcceptanceCheckResult(
                    check,
                    target,
                    False,
                    int(progress_response.id),
                    "complex task did not expose a bounded approach before its recommendation",
                )

            filename = "hub-contract-v2-e2e.md"
            artifact_request = await client.send_message(
                config.telegram_chat_id,
                (
                    f"@{target} Create {filename} in the exact Hub artifact delivery directory "
                    "for this turn. Its complete UTF-8 content must be "
                    "HUB_CONTRACT_V2_E2E_OK followed by one newline. Reply briefly; do no "
                    "other work."
                ),
                reply_to=config.telegram_thread_id,
            )
            artifact_response = await _wait_for_response(
                client,
                config,
                after_id=int(artifact_request.id),
                username=target,
                require_document=True,
            )
            received_name = str(getattr(getattr(artifact_response, "file", None), "name", ""))
            payload = await artifact_response.download_media(file=bytes)
            if received_name != filename or payload != b"HUB_CONTRACT_V2_E2E_OK\n":
                return AcceptanceCheckResult(
                    check,
                    target,
                    False,
                    int(artifact_response.id),
                    "artifact task returned an unexpected document",
                )
        except AcceptanceActorError as exc:
            return AcceptanceCheckResult(check, target, False, None, str(exc))
        return AcceptanceCheckResult(
            check,
            target,
            True,
            int(artifact_response.id),
            "short, clarification, complex-progress, and artifact behavior verified",
        )
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
            context_text = str(context_reply.raw_text).strip()
            if not context_text or any(
                marker in context_text.casefold()
                for marker in (
                    "no matching prior dialogue",
                    "no prior dialogue is stored",
                    "no matching history",
                )
            ):
                raise AcceptanceActorError("explicit context was reported missing")
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
        ok = _stop_acknowledged(stop_text) and "AFTER_STOP_E2E_OK" in response_text
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
        ok = any(
            phrase in response_text
            for phrase in ("will start on the next message", "already active", "is now active")
        )
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
            if check == "p0_p1_live":
                live_results = await _run_p0_p1_live_checks(client, config, target)
                results.extend(live_results)
                if any(not result.ok for result in live_results):
                    return results
                continue
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
    if check in {"codex_interaction_v2", "p0_p1_live"}:
        try:
            index = config.provider_agent_ids.index("codex")
            return (config.provider_usernames[index],)
        except (IndexError, ValueError) as exc:
            raise AcceptanceActorError(
                f"{check} requires one aligned codex provider identity"
            ) from exc
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
