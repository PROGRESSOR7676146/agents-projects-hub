from __future__ import annotations

import asyncio
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .acceptance_contracts import (
    AcceptanceActorConfig,
    AcceptanceActorError,
    AcceptanceCheckResult,
)
from .acceptance_runtime import (
    AcceptanceRuntimeError,
    FixedServiceSupervisor,
    ReadOnlyAcceptanceState,
    ServiceSnapshot,
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


@dataclass(frozen=True, slots=True)
class P0P1ScenarioContext:
    client: Any
    config: AcceptanceActorConfig
    state_probe: ReadOnlyAcceptanceState
    service_supervisor: FixedServiceSupervisor


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
                if require_buttons and not getattr(message, "buttons", None):
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
            value = getattr(button, "data", None)
            if isinstance(value, bytes) and value.split(b"~", 1)[0] == data:
                await button.click()
                return
    raise AcceptanceActorError(
        f"model menu has no {data.decode('ascii', errors='replace')} callback"
    )


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


async def run_p0_p1_live_scenario(
    context: P0P1ScenarioContext,
    target: str,
) -> list[AcceptanceCheckResult]:
    client = context.client
    config = context.config
    state = context.state_probe
    services = context.service_supervisor
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
