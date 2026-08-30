from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timezone

from .hub_config import HubConfig
from .state import HubState
from .telegram import TelegramBotApi


class TelegramOutboxSenderError(RuntimeError):
    pass


class TelegramOutboxSender:
    """Deliver durable provider results without owning any provider runtime."""

    _LOCAL_QUEUE_RUNTIMES = frozenset({"codex", "gemini", "opencode", "antigravity"})

    def __init__(
        self,
        config: HubConfig,
        *,
        telegram_bots: dict[str, TelegramBotApi] | None = None,
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
            bots: dict[str, TelegramBotApi] = {}
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
        self.telegram_bots = telegram_bots
        self.sender_id = sender_id
        self.state = HubState.open(config.state_path)
        self._cursor = 0
        self._stop = threading.Event()
        self._started_at = datetime.now(timezone.utc)
        self._process_start_marker = uuid.uuid4().hex
        self._last_success_at: datetime | None = None
        self._last_error_code: str | None = None
        self._last_health_publish_monotonic = 0.0
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
            )
            self._last_health_publish_monotonic = now_monotonic
        except Exception:
            pass

    def run_forever(self, *, poll_seconds: float = 0.2) -> None:
        if poll_seconds <= 0:
            raise TelegramOutboxSenderError("poll_seconds must be positive")
        try:
            while not self._stop.is_set():
                try:
                    worked = self.run_cycle()
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

    def run_cycle(self) -> bool:
        """Recover stale leases and fairly deliver at most one prepared row."""
        if self._stop.is_set():
            return False
        self._publish_health()
        self.state.recover_stale_telegram_outbox(sender_agent_ids=self.agent_ids)
        start = self._cursor % len(self.agent_ids)
        for offset in range(len(self.agent_ids)):
            if self._stop.is_set():
                return False
            position = (start + offset) % len(self.agent_ids)
            agent_id = self.agent_ids[position]
            if self._deliver_one(agent_id):
                self._cursor = (position + 1) % len(self.agent_ids)
                return True
        return False

    def _deliver_one(self, agent_id: str) -> bool:
        outbox = self.state.lease_telegram_outbox(agent_id, self.sender_id)
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
            message_id = self.telegram_bots[agent_id].send_html(
                outbox.chat_id, outbox.thread_id, outbox.telegram_html
            )
            self.state.mark_telegram_outbox_delivered(
                outbox.outbox_id,
                outbox.lease_token,
                telegram_message_id=message_id or 1,
            )
            self._last_success_at = datetime.now(timezone.utc)
            self._last_error_code = None
        except Exception as exc:
            self._last_error_code = type(exc).__name__[:128]
            self.state.retry_telegram_outbox(
                outbox.outbox_id,
                outbox.lease_token,
                error_code=type(exc).__name__,
                delay_seconds=1,
            )
        self._publish_health(force=True)
        return True
