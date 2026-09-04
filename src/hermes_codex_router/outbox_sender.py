from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .artifacts import (
    artifact_spool_root,
    remove_spooled_artifact,
    verify_spooled_artifact,
)
from .hub_config import HubConfig
from .state import HubState
from .telegram import TelegramBotApi, TelegramError


class TelegramOutboxSenderError(RuntimeError):
    pass


class TelegramSender(Protocol):
    def send_chat_action(self, chat_id: int, thread_id: int, action: str = "typing") -> None: ...
    def send_html(self, chat_id: int, thread_id: int, html: str) -> int: ...
    def send_document(
        self,
        chat_id: int,
        thread_id: int,
        document_path: Path,
        *,
        caption: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        file_name: str | None = None,
        mime_type: str | None = None,
    ) -> int: ...
    def send_message_draft(
        self, chat_id: int, thread_id: int, *, draft_id: int, text: str = ""
    ) -> None: ...


class TelegramOutboxSender:
    """Deliver durable provider results without owning any provider runtime."""

    _LOCAL_QUEUE_RUNTIMES = frozenset({"codex", "gemini", "opencode", "antigravity"})
    _CHAT_ACTION_INTERVAL_SECONDS = 4.0

    def __init__(
        self,
        config: HubConfig,
        *,
        telegram_bots: Mapping[str, TelegramSender] | None = None,
        sender_id: str = "telegram-outbox-sender",
    ) -> None:
        if (
            config.dispatch_mode != "queue"
            or config.queue_runtime != "external"
            or config.outbox_runtime != "external"
        ):
            raise TelegramOutboxSenderError(
                "outbox sender requires queue dispatch with external outbox runtime"
            )
        # External delivery is one ownership boundary for the entire shared
        # queue, including providers whose execution remains embedded during a
        # mixed rollout. Otherwise their committed outbox rows would be
        # stranded when the compatibility controller sender is disabled.
        self.agent_ids = tuple(
            agent.agent_id
            for agent in config.agents
            if not agent.managed_externally and agent.runtime in self._LOCAL_QUEUE_RUNTIMES
        )
        if not self.agent_ids:
            raise TelegramOutboxSenderError("external outbox has no locally managed agents")
        if telegram_bots is None:
            bots: dict[str, TelegramSender] = {}
            for agent_id in self.agent_ids:
                agent = config.require_agent(agent_id)
                if agent.token_file is None:
                    raise TelegramOutboxSenderError(
                        f"external worker agent {agent_id} requires a Telegram token"
                    )
                if not agent.token_file.is_file() or agent.token_file.stat().st_mode & 0o077:
                    raise TelegramOutboxSenderError(
                        f"Telegram token for external worker agent {agent_id} must be private"
                    )
                token = agent.token_file.read_text(encoding="utf-8").strip()
                bots[agent_id] = TelegramBotApi(token)
            telegram_bots = bots
        missing = [agent_id for agent_id in self.agent_ids if agent_id not in telegram_bots]
        if missing:
            raise TelegramOutboxSenderError(
                f"missing Telegram sender identity for agent: {missing[0]}"
            )
        self.config = config
        self.telegram_bots = dict(telegram_bots)
        self.sender_id = sender_id
        self.state = HubState.open(config.state_path)
        self._cursor = 0
        self._stop = threading.Event()
        self._started_at = datetime.now(timezone.utc)
        self._process_start_marker = uuid.uuid4().hex
        self._last_success_at: datetime | None = None
        self._last_error_code: str | None = None
        self._transport_error: TelegramError | None = None
        self._transport_consecutive_failures = 0
        self._transport_success_at: datetime | None = None
        self._transport_reported_signature: tuple[str, str, int | None] | None = None
        self._last_health_publish_monotonic = 0.0
        self._chat_action_due: dict[tuple[str, int, int], float] = {}
        self._chat_action_failures: dict[tuple[str, int, int], tuple[str, str, int | None]] = {}
        self._publish_health()

    def close(self) -> None:
        self.stop()
        self.state.close()

    def stop(self) -> None:
        self._stop.set()

    def _record_event(self, level: str, code: str, detail: str) -> None:
        try:
            self.state.record_runtime_event("outbox", level, code, detail)
        except Exception:
            pass

    def _publish_health(
        self,
        *,
        activity_state: str = "idle",
        active_outbox_id: str | None = None,
        active_lease_expires_at: datetime | None = None,
        force: bool = False,
    ) -> None:
        """Publish bounded local state without allowing telemetry to stop delivery."""
        now_monotonic = time.monotonic()
        if not force and now_monotonic - self._last_health_publish_monotonic < 10.0:
            return
        try:
            self.state.upsert_runtime_health(
                component="sender",
                instance_id=self.sender_id,
                runtime="telegram",
                agent_id=None,
                pid=os.getpid(),
                process_start_marker=self._process_start_marker,
                started_at=self._started_at,
                heartbeat_at=datetime.now(timezone.utc),
                success_at=self._last_success_at,
                error_code=self._last_error_code,
                activity_state=activity_state,
                active_job_id=active_outbox_id,
                active_lease_expires_at=active_lease_expires_at,
                transport_operation=(
                    None if self._transport_error is None else self._transport_error.operation
                ),
                transport_failure_class=(
                    None if self._transport_error is None else self._transport_error.failure_class
                ),
                transport_status_code=(
                    None if self._transport_error is None else self._transport_error.status_code
                ),
                transport_retry_after=(
                    None if self._transport_error is None else self._transport_error.retry_after
                ),
                transport_consecutive_failures=self._transport_consecutive_failures,
                transport_success_at=self._transport_success_at,
            )
            self._last_health_publish_monotonic = now_monotonic
        except Exception:
            pass

    def _record_transport_failure(self, error: TelegramError) -> None:
        self._transport_consecutive_failures += 1
        self._transport_error = error
        self._last_error_code = error.health_code
        if error.signature != self._transport_reported_signature:
            self._record_event(
                "warning",
                "telegram_transport_error",
                error.safe_detail(
                    consecutive_failures=self._transport_consecutive_failures,
                    last_success=(
                        None
                        if self._transport_success_at is None
                        else self._transport_success_at.isoformat()
                    ),
                ),
            )
            self._transport_reported_signature = error.signature

    def _record_transport_success(self) -> None:
        observed_at = datetime.now(timezone.utc)
        failures = self._transport_consecutive_failures
        recovered_operation = (
            "send" if self._transport_error is None else self._transport_error.operation
        )
        if failures:
            self._record_event(
                "info",
                "telegram_recovered",
                f"operation={recovered_operation};consecutive_failures={failures};"
                f"last_success={observed_at.isoformat()}",
            )
        self._transport_error = None
        self._transport_consecutive_failures = 0
        self._transport_success_at = observed_at
        self._transport_reported_signature = None
        self._last_success_at = observed_at
        self._last_error_code = None

    def run_forever(self, *, poll_seconds: float = 0.2) -> None:
        if poll_seconds <= 0:
            raise TelegramOutboxSenderError("poll_seconds must be positive")
        try:
            while not self._stop.is_set():
                try:
                    worked = self.run_cycle()
                    if self._last_error_code == "sender_cycle_error":
                        # A complete cycle proves that the queue/SQLite path
                        # recovered even when there was nothing to deliver.
                        self._last_success_at = datetime.now(timezone.utc)
                        self._last_error_code = None
                        self._publish_health(force=True)
                except Exception as exc:
                    self._record_event("error", "sender_cycle_error", type(exc).__name__)
                    self._last_error_code = "sender_cycle_error"
                    self._publish_health(force=True)
                    worked = False
                # Keep the heartbeat fresh even when an operator chooses a very
                # long idle polling interval.
                self._stop.wait(0.01 if worked else min(poll_seconds, 30.0))
        except KeyboardInterrupt:
            return

    def run_cycle(self, *, now: datetime | None = None) -> bool:
        """Recover stale leases and fairly deliver at most one prepared row."""
        if self._stop.is_set():
            return False
        self._publish_health()
        self.state.recover_stale_telegram_outbox(sender_agent_ids=self.agent_ids, now=now)
        start = self._cursor % len(self.agent_ids)
        for offset in range(len(self.agent_ids)):
            if self._stop.is_set():
                return False
            position = (start + offset) % len(self.agent_ids)
            agent_id = self.agent_ids[position]
            if self._deliver_one(agent_id, now=now):
                self._cursor = (position + 1) % len(self.agent_ids)
                return True
        # Result delivery always has priority over an advisory chat action.
        self._refresh_chat_actions()
        return False

    def _refresh_chat_actions(self, *, now_monotonic: float | None = None) -> None:
        """Best-effort provider-identity typing indicators for accepted work."""
        current = time.monotonic() if now_monotonic is None else now_monotonic
        activities = self.state.provider_chat_activities(self.agent_ids)
        active_keys = {
            (activity.agent_id, activity.chat_id, activity.thread_id) for activity in activities
        }
        self._chat_action_due = {
            key: due for key, due in self._chat_action_due.items() if key in active_keys
        }
        self._chat_action_failures = {
            key: signature
            for key, signature in self._chat_action_failures.items()
            if key in active_keys
        }
        for activity in activities:
            key = (activity.agent_id, activity.chat_id, activity.thread_id)
            if current < self._chat_action_due.get(key, 0.0):
                continue
            try:
                telegram = self.telegram_bots[activity.agent_id]
                telegram.send_chat_action(activity.chat_id, activity.thread_id)
                if activity.chat_id > 0:
                    try:
                        telegram.send_message_draft(
                            activity.chat_id,
                            activity.thread_id,
                            draft_id=activity.message_id,
                        )
                    except Exception:
                        pass
            except Exception as exc:
                error = (
                    exc
                    if isinstance(exc, TelegramError)
                    else TelegramError(
                        "Telegram advisory request failed",
                        operation="chat_action",
                        failure_class="unexpected_client",
                    )
                )
                if self._chat_action_failures.get(key) != error.signature:
                    self._record_event(
                        "warning",
                        "chat_action_error",
                        error.safe_detail(consecutive_failures=1, last_success=None),
                    )
                    self._chat_action_failures[key] = error.signature
            else:
                if self._chat_action_failures.pop(key, None) is not None:
                    self._record_event("info", "chat_action_recovered", "operation=chat_action")
            self._chat_action_due[key] = current + self._CHAT_ACTION_INTERVAL_SECONDS

    def _deliver_one(self, agent_id: str, *, now: datetime | None = None) -> bool:
        outbox = self.state.lease_telegram_outbox(agent_id, self.sender_id, now=now)
        if outbox is None or outbox.lease_token is None:
            return False
        if self._stop.is_set():
            self.state.release_telegram_outbox_lease(outbox.outbox_id, outbox.lease_token)
            return False
        lease_expires_at = (
            None
            if outbox.lease_expires_at is None
            else datetime.fromisoformat(outbox.lease_expires_at)
        )
        self._publish_health(
            activity_state="sending",
            active_outbox_id=outbox.outbox_id,
            active_lease_expires_at=lease_expires_at,
            force=True,
        )
        try:
            part = self.state.next_telegram_outbox_part(
                outbox.outbox_id, outbox.lease_token, now=now
            )
            delivered_file: Path | None = None
            if part.part_type == "document":
                if (
                    not part.file_path
                    or not part.file_name
                    or part.file_size is None
                    or part.file_sha256 is None
                ):
                    raise TelegramOutboxSenderError("artifact outbox metadata is incomplete")
                file_path = Path(part.file_path)
                spool_root = artifact_spool_root(self.config.state_path)
                verify_spooled_artifact(
                    file_path,
                    spool_root,
                    expected_size=part.file_size,
                    expected_sha256=part.file_sha256,
                )
                message_id = self.telegram_bots[agent_id].send_document(
                    outbox.chat_id,
                    outbox.thread_id,
                    file_path,
                    caption=part.telegram_html or None,
                    file_name=part.file_name,
                )
                delivered_file = file_path
            else:
                message_id = self.telegram_bots[agent_id].send_html(
                    outbox.chat_id, outbox.thread_id, part.telegram_html
                )
            self.state.mark_telegram_outbox_delivered(
                outbox.outbox_id,
                outbox.lease_token,
                telegram_message_id=message_id or 1,
                now=now,
            )
            self._record_transport_success()
            if delivered_file is not None:
                try:
                    remove_spooled_artifact(
                        delivered_file, artifact_spool_root(self.config.state_path)
                    )
                except Exception as exc:
                    self._record_event("warning", "artifact_cleanup_error", type(exc).__name__)
        except Exception as exc:
            error_code = type(exc).__name__[:128]
            if isinstance(exc, TelegramError):
                self._record_transport_failure(exc)
                error_code = exc.health_code
            else:
                self._last_error_code = error_code
            self.state.retry_telegram_outbox(
                outbox.outbox_id,
                outbox.lease_token,
                error_code=error_code,
                delay_seconds=1,
                now=now,
            )
        self._publish_health(force=True)
        return True
