from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

from hermes_codex_router.external_runtime import ExternalTurnResult
from tests.fault_matrix_support import FaultMatrixHarness, RecordingBot


class MarkerAdapter:
    def __init__(self, runtime: str, marker: Path, *, block: bool) -> None:
        self.runtime = runtime
        self.marker = marker
        self.block = block

    def run_turn(self, **kwargs: object) -> ExternalTurnResult:
        with self.marker.open("a", encoding="utf-8") as stream:
            stream.write("invoked\n")
        if self.block:
            threading.Event().wait()
        prompt = str(kwargs["prompt"])
        return ExternalTurnResult(
            self.runtime,
            f"{self.runtime} completed: {prompt}",
            f"{self.runtime}-fictional-session",
            "fictional-model",
        )


class BlockingBot(RecordingBot):
    def __init__(self, marker: Path) -> None:
        super().__init__()
        self.marker = marker

    def send_html(self, chat_id: int, thread_id: int, html: str, **kwargs: object) -> int:
        super().send_html(chat_id, thread_id, html, **kwargs)
        self.marker.write_text("accepted", encoding="utf-8")
        threading.Event().wait()
        return 1


class MarkerBot(RecordingBot):
    def __init__(self, marker: Path) -> None:
        super().__init__()
        self.marker = marker

    def send_html(self, chat_id: int, thread_id: int, html: str, **kwargs: object) -> int:
        result = super().send_html(chat_id, thread_id, html, **kwargs)
        self.marker.write_text(html, encoding="utf-8")
        return result


class OneUpdatePoller(MarkerBot):
    def __init__(
        self, marker: Path, update: dict[str, object], *, record_offset: bool = False
    ) -> None:
        super().__init__(marker)
        self.update = update
        self.service: Any = None
        self.poll_count = 0
        self.record_offset = record_offset

    def updates(self, *, offset: int | None, timeout: int) -> list[dict[str, object]]:
        del timeout
        self.poll_count += 1
        if self.poll_count == 1:
            if self.record_offset:
                self.marker.write_text(f"offset:{offset}", encoding="utf-8")
            return [self.update]
        if not self.marker.exists():
            self.marker.write_text("poll-complete", encoding="utf-8")
        self.service.stop()
        return []


def controller_once(harness: FaultMatrixHarness, arguments: list[str]) -> None:
    identity, admission, raw_message_id, raw_thread_id, text, raw_marker = arguments
    service = harness.controller(
        ingress_identity=identity,
        direct_messages_only=admission == "direct",
    )
    poller = OneUpdatePoller(
        Path(raw_marker),
        harness.update(int(raw_message_id), int(raw_thread_id), text),
        record_offset=True,
    )
    poller.service = service
    service.telegram = cast(Any, poller)
    try:
        service.run_forever()
    finally:
        service.state.close()


def controller_block_after_enqueue(harness: FaultMatrixHarness, arguments: list[str]) -> None:
    raw_message_id, raw_thread_id, text, raw_marker = arguments
    marker = Path(raw_marker)
    service = harness.controller()
    poller = OneUpdatePoller(
        marker,
        harness.update(int(raw_message_id), int(raw_thread_id), text),
    )
    poller.service = service
    service.telegram = cast(Any, poller)
    original = service.handle_update

    def handle_then_block(update: dict[str, object]) -> bool:
        handled = original(update)
        marker.write_text("enqueued-before-offset", encoding="utf-8")
        threading.Event().wait()
        return handled

    service.handle_update = handle_then_block  # type: ignore[method-assign]
    service.run_forever()


def worker_actor(harness: FaultMatrixHarness, mode: str, arguments: list[str]) -> None:
    agent_id, raw_marker = arguments
    marker = Path(raw_marker)
    adapter = MarkerAdapter(agent_id, marker, block=mode == "worker-block-in-adapter")
    worker = harness.worker(agent_id, adapter)
    if mode == "worker-block-after-lease":
        job = worker.state.lease_provider_job(agent_id, worker.worker_id)
        if job is None:
            raise RuntimeError("fictional worker found no job to lease")
        marker.write_text("leased-before-execution", encoding="utf-8")
        threading.Event().wait()
    try:
        if not worker.run_cycle():
            raise RuntimeError("fictional worker found no executable job")
    finally:
        worker.close()


def sender_actor(harness: FaultMatrixHarness, mode: str, arguments: list[str]) -> None:
    marker = Path(arguments[0])
    clock_offset_seconds = int(arguments[1]) if len(arguments) > 1 else 0
    now = datetime.now(timezone.utc) + timedelta(seconds=clock_offset_seconds)
    bot: object = (
        BlockingBot(marker) if mode == "sender-block-after-acceptance" else MarkerBot(marker)
    )
    sender = harness.sender(opencode=bot, antigravity=bot, codex=bot)
    try:
        if not sender.run_cycle(now=now):
            raise RuntimeError("fictional sender found no prepared outbox")
    finally:
        sender.close()


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise RuntimeError("fault actor mode and base path are required")
    mode, raw_base, *arguments = argv
    harness = FaultMatrixHarness(Path(raw_base))
    if mode == "controller-once":
        controller_once(harness, arguments)
    elif mode == "controller-block-after-enqueue":
        controller_block_after_enqueue(harness, arguments)
    elif mode.startswith("worker-"):
        worker_actor(harness, mode, arguments)
    elif mode.startswith("sender-"):
        sender_actor(harness, mode, arguments)
    else:
        raise RuntimeError(f"unknown fault actor mode: {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
