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
    _CHAT_ACTION_INTERVAL_SECONDS = 4.0

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
        self._chat_action_due: dict[tuple[str, int, int], float] = {}
        self._chat_action_failures: set[tuple[str, int, int]] = set()
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
        self._chat_action_failures.intersection_update(active_keys)
        for activity in activities:
            key = (activity.agent_id, activity.chat_id, activity.thread_id)
            if current < self._chat_action_due.get(key, 0.0):
                continue
            try:
                telegram = self.telegram_bots[activity.agent_id]
                if activity.chat_id > 0:
                    telegram.send_message_draft(
                        activity.chat_id,
                        activity.thread_id,
                        draft_id=activity.message_id,
                    )
                else:
                    telegram.send_chat_action(activity.chat_id, activity.thread_id)
            except Exception as exc:
                if key not in self._chat_action_failures:
                    self._record_event(
                        "warning",
                        "chat_action_error",
                        f"{activity.agent_id}:{type(exc).__name__}",
                    )
                    self._chat_action_failures.add(key)
            else:
                self._chat_action_failures.discard(key)
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
            message_id = self.telegram_bots[agent_id].send_html(
                outbox.chat_id, outbox.thread_id, outbox.telegram_html
            )
            self.state.mark_telegram_outbox_delivered(
                outbox.outbox_id,
                outbox.lease_token,
                telegram_message_id=message_id or 1,
                now=now,
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
                now=now,
            )
        self._publish_health(force=True)
        return True
